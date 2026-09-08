# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the room-correction wizard at /sound/room/.

The page started as the Phase 0 mic-permission skeleton and has grown
into the full correction wizard, so this file pins both browser-facing
HTML/JS contracts and real HTTP dispatch:

  1. Page render — hostname substitutes through, sample-rate constant
     reaches the JS, the placement advice is present, and the local
     certificate guidance stays to one sentence.
  2. Healthz returns plain-text "ok" so systemd / curl probes work.
  3. End-to-end via a real ThreadingHTTPServer to confirm the routes
     dispatch from real HTTP — same shape as test_voice_setup.

Keep the existing test names where possible so future-me can grep for
the original Phase 0 pins.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import io
import inspect
import json
import logging
from contextlib import contextmanager, nullcontext
from types import MappingProxyType, SimpleNamespace
import threading
import urllib.error
import urllib.request
from email.message import Message
from http.server import ThreadingHTTPServer

import pytest

from pathlib import Path

from jasper.web import (
    correction_capture,
    correction_handlers,
    correction_setup,
    correction_tuning,
)
from jasper.platform.systemd import no_hold
from jasper.active_speaker.runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GraphSafety,
)

from ._async_wait import DEFAULT_SIGNAL_TIMEOUT_S, wait_until_sync
from ._web_test_helpers import request_with_csrf
from .correction_session_fixtures import make_measurement_session


@pytest.fixture(autouse=True)
def _stable_no_bass_graph_authority(monkeypatch):
    async def classify(_cam):
        return GraphSafety(
            classification=GRAPH_APPROVED_ACTIVE_RUNTIME,
            allowed=True,
            details={
                "bass_extension_profile_summary": {
                    "authority_valid": True,
                    "runtime_block_required": False,
                }
            },
        )

    monkeypatch.setattr(
        correction_capture,
        "_classify_live_bass_extension_graph",
        classify,
    )

# The page's behaviour ships as a static ES module under
# deploy/assets/correction/js/. What that module DOES is pinned by
# tests/js/correction_render_harness.mjs, which evaluates it; the tests here
# pin only the server-rendered markup the module reaches for.

# ---------- Page render ----------------------------------------------------


def test_render_page_substitutes_hostname():
    body = correction_setup._render_page("acoustic-lab.local").decode()
    assert "acoustic-lab.local" in body
    # The hostname appears in the absolute HTTP dashboard back link.
    assert "__HOSTNAME__" not in body


def test_render_page_no_unfilled_placeholders():
    """Defensive: catch any future placeholder that gets added to
    _PAGE_HTML but forgotten in _render_page."""
    body = correction_setup._render_page("jts.local").decode()
    assert "__STYLE__" not in body
    assert "__HOSTNAME__" not in body
    assert "__REQUIRED_SR__" not in body
    assert "__CSRF_META__" not in body
    assert "__CSRF_FETCH_HELPERS__" not in body
    assert "__TARGET_PROFILE_OPTIONS__" not in body
    assert "__CORRECTION_STRATEGY_OPTIONS__" not in body


def test_render_page_embeds_csrf_meta():
    # The module reads its token from this tag; the harness pins that every
    # mutating request carries the header helper's output.
    body = correction_setup._render_page("jts.local", "csrf-token").decode()
    assert 'meta name="jts-csrf" content="csrf-token"' in body


def test_capture_stop_holds_slot_until_owner_cleanup_is_terminal():
    stop_event = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def open_capture():
        return SimpleNamespace(pi_session=object())

    async def run_and_consume(_pi_session):
        await asyncio.to_thread(stop_event.wait)
        cleanup_started.set()
        await asyncio.to_thread(release_cleanup.wait)

    correction_capture._set_capture_slot(None)
    try:
        correction_capture._run_capture(
            correction_capture.CaptureKind(
                label="crossover_sweep:driver",
                open=open_capture,
                run_and_consume=run_and_consume,
                request_stop=stop_event.set,
            ),
            idle_hold=no_hold,
        )
        response = correction_capture._request_capture_stop("crossover_sweep:")
        assert response["status"] == "stopping"
        assert stop_event.is_set()
        assert cleanup_started.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        assert correction_capture._get_capture_slot()["status"] == "stopping"
        assert not correction_capture._begin_capture_slot("crossover_sweep:summed")
        release_cleanup.set()
        wait_until_sync(
            lambda: correction_capture._get_capture_slot()["status"] == "stopped"
        )
        assert correction_capture._get_capture_slot()["status"] == "stopped"
    finally:
        release_cleanup.set()


class _RecordingIdleHold:
    """Stand-in for ``IdleShutdownTracker.hold`` that counts acquire/release.

    Same shape as the real seam — call it with a label, get a context manager —
    so a test can assert the pairing without a live tracker or a real timer
    thread.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.active = 0

    def __call__(self, label: str):
        @contextmanager
        def _cm():
            self.events.append(("acquire", label))
            self.active += 1
            try:
                yield
            finally:
                self.active -= 1
                self.events.append(("release", label))

        return _cm()

    @property
    def labels(self) -> list[str]:
        return [label for _kind, label in self.events]


def test_the_capture_spawn_seam_has_no_silent_idle_hold_default():
    """Whether a capture runner outlives its request is a per-call-site decision.

    ``_run_capture``'s job IS spawning work that outlives the POST, and
    the socket-activated process exits after ~600 s with nothing inbound
    (#1854). A default — safe or unsafe — makes that decision invisible and
    lets the next call site inherit it silently. Required keyword-only means a
    site that forgets fails at the call, not on a household's speaker.
    """
    param = inspect.signature(correction_capture._run_capture).parameters["idle_hold"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty, (
        "idle_hold must stay required — pass _systemd.no_hold to opt out "
        "explicitly"
    )


def test_capture_holds_the_idle_exit_for_the_whole_background_session():
    """The background runner keeps the socket-activated wizard alive (#1854).

    2026-07-29 JTS3: a crossover-v2 session's last INBOUND request was the
    envelope GET the phone made before it navigated to the capture origin.
    Everything after that — status polling, sweep playback, analysis, apply,
    verify — ran on background workers holding nothing, so correction-web's
    600 s idle exit fired mid-verify and `os._exit(0)`'d the analysis away.
    The hold is taken on the request thread before the runner is scheduled and
    released only when the runner reaches a terminal state.
    """
    idle_hold = _RecordingIdleHold()
    release_runner = threading.Event()
    runner_entered = threading.Event()

    def open_capture():
        return SimpleNamespace(pi_session=object())

    async def run_and_consume(_pi_session):
        runner_entered.set()
        await asyncio.to_thread(release_runner.wait)

    correction_capture._set_capture_slot(None)
    try:
        correction_capture._run_capture(
            correction_capture.CaptureKind(
                label="crossover_v2:session",
                open=open_capture,
                run_and_consume=run_and_consume,
            ),
            idle_hold=idle_hold,
        )
        # Held from the moment the POST returns — before the runner has even
        # been scheduled, which is the window a phone-only session sits in.
        assert idle_hold.events == [("acquire", "capture:crossover_v2:session")]
        assert runner_entered.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        assert idle_hold.active == 1

        release_runner.set()
        wait_until_sync(
            lambda: correction_capture._get_capture_slot()["status"] == "complete"
        )
        assert correction_capture._get_capture_slot()["status"] == "complete"
    finally:
        release_runner.set()
        correction_capture._set_capture_slot(None)

    wait_until_sync(lambda: not idle_hold.active)
    assert idle_hold.active == 0, "the completed session released its hold"
    assert idle_hold.events == [
        ("acquire", "capture:crossover_v2:session"),
        ("release", "capture:crossover_v2:session"),
    ]


def test_capture_releases_the_idle_hold_when_the_runner_fails():
    """Every terminal path releases — failure included (#1854).

    A hold that only released on the happy path would trade a killed session
    for an immortal wizard, and the capture runner's ordinary endings (user stop,
    capture timeout, begin-refused, the catch-all cleanup arm) are ALL exception
    paths.
    """
    idle_hold = _RecordingIdleHold()

    def open_capture():
        return SimpleNamespace(pi_session=object())

    async def run_and_consume(_pi_session):
        raise RuntimeError("the measurement link timed out")

    correction_capture._set_capture_slot(None)
    try:
        correction_capture._run_capture(
            correction_capture.CaptureKind(
                label="crossover_v2:verify",
                open=open_capture,
                run_and_consume=run_and_consume,
            ),
            idle_hold=idle_hold,
        )
        wait_until_sync(
            lambda: correction_capture._get_capture_slot()["status"] == "failed"
        )
    finally:
        correction_capture._set_capture_slot(None)

    wait_until_sync(lambda: not idle_hold.active)
    assert idle_hold.active == 0
    assert idle_hold.events == [
        ("acquire", "capture:crossover_v2:verify"),
        ("release", "capture:crossover_v2:verify"),
    ]


def test_capture_drops_the_idle_hold_when_the_runner_never_spawns(
    monkeypatch,
):
    """A failed spawn must not leave a hold nobody will ever release."""
    idle_hold = _RecordingIdleHold()

    def open_capture():
        return SimpleNamespace(pi_session=object())

    async def run_and_consume(_pi_session):
        raise AssertionError("never scheduled")

    def _refuse(coro, _loop):
        coro.close()
        raise RuntimeError("event loop is closed")

    monkeypatch.setattr(
        correction_setup.asyncio, "run_coroutine_threadsafe", _refuse,
    )
    correction_capture._set_capture_slot(None)
    try:
        with pytest.raises(RuntimeError, match="event loop is closed"):
            correction_capture._run_capture(
                correction_capture.CaptureKind(
                    label="crossover_v2:session",
                    open=open_capture,
                    run_and_consume=run_and_consume,
                ),
                idle_hold=idle_hold,
            )
    finally:
        correction_capture._set_capture_slot(None)

    assert idle_hold.active == 0
    assert idle_hold.labels == [
        "capture:crossover_v2:session", "capture:crossover_v2:session",
    ]


def test_the_v2_dispatch_threads_the_idle_hold_into_the_capture_runner(
    monkeypatch,
):
    """The one background lifetime a v2 session still owns (#1854).

    RE-DERIVED by PR-T3: this used to assert BOTH lifetimes — the capture
    runner, held by ``_run_capture``, and the auto-apply worker thread
    the preparer spawned, which could outlive it. The two-stage split removed
    that worker: the apply is now a household POST served in-request, so the
    idle tracker's ordinary in-flight-request accounting holds the process for
    it and the preparers take no ``idle_hold`` at all. What remains is the
    runner's hold, which is still the one #1854 was actually about.
    """
    idle_hold = _RecordingIdleHold()
    seen: dict[str, object] = {}

    def _fake_prepare(raw, *, status, run_async, camilla_factory, verify_only):
        seen["prepare_kwargs"] = {
            "status", "run_async", "camilla_factory", "verify_only",
        }
        return SimpleNamespace(
            label="crossover_v2:session",
            open=lambda *a, **kw: None,
            run_and_consume=lambda *a, **kw: None,
            request_stop=lambda: None,
            # An ungated session carries no position gate; the field is
            # stated rather than omitted so this stub keeps matching the real
            # V2PreparedSession the dispatch reads.
            position_gate=None,
            # #2662 W2b: the dispatch forwards the session's two local signals
            # — the all-spots-measured confirmation and the per-take retake
            # (#2879). Stated rather than omitted so this stub keeps matching
            # the real V2PreparedSession.
            request_complete=None,
            request_retake=None,
        )

    def _fake_run_capture(kind, *, idle_hold):
        seen["orchestrator"] = idle_hold
        return {"status": "awaiting_capture"}

    from jasper.web import correction_crossover_backend
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(correction_capture, "_read_json_body", lambda _h: {})
    monkeypatch.setattr(correction_capture, "_crossover_blocking_phase", lambda: None)
    monkeypatch.setattr(correction_crossover_backend, "status_payload", dict)
    monkeypatch.setattr(v2host, "prepare_v2_session", _fake_prepare)
    monkeypatch.setattr(correction_capture, "_run_capture", _fake_run_capture)

    correction_handlers._handle_crossover_v2_capture(
        None, verify_only=False, idle_hold=idle_hold,
    )

    # The preparer takes no hold any more — and cannot silently regrow one
    # unnoticed, because a stub that accepted extra kwargs would still fail
    # this signature check.
    assert "idle_hold" not in seen["prepare_kwargs"]
    assert "idle_hold" not in inspect.signature(v2host.prepare_v2_session).parameters
    assert seen["orchestrator"] is idle_hold

    # ...and the route reads it off the handler class make_server binds.
    # (main() handing tracker.hold to make_server is pinned at runtime by
    # test_web_correction_setup::test_main_wires_idle_tracker_to_capture_entry_restore.)
    from jasper.platform import systemd as _systemd

    built: dict[str, object] = {}
    monkeypatch.setattr(
        _systemd,
        "make_http_server",
        lambda _target, handler_cls: built.setdefault("cls", handler_cls),
    )
    correction_setup.make_server(0, idle_hold=idle_hold)
    assert built["cls"].idle_hold is idle_hold


@pytest.mark.parametrize(
    ("verify_only", "expected_label"),
    [
        pytest.param(False, "crossover_v2:session", id="session-route"),
        pytest.param(True, "crossover_v2:verify", id="verify-route"),
    ],
)
def test_the_v2_dispatch_carries_its_routes_stage_into_the_capture_kind(
    monkeypatch, verify_only, expected_label,
):
    """Which STAGE a route opens, carried through the dispatch to the kind.

    ``/crossover/v2/session`` and ``/crossover/v2/verify`` are one handler
    separated by one boolean, and since the two preparers converged that boolean
    is the whole of the separation. Nothing pinned it: hardcoding
    ``verify_only=False`` at the call site passed every suite, because the
    handler had only ever been driven for stage 1.

    Asserted at BOTH ends of the hop — the flag the preparer is handed, and the
    label the capture kind ends up carrying — so neither a dropped argument nor a
    preparer that ignores it can pass. The expected labels are spelled as
    literals rather than read back off the module, because they are the wire
    identity the capture lifecycle keys on.
    """
    from jasper.web import correction_crossover_backend
    from jasper.web import correction_crossover_v2 as v2host

    seen: dict[str, object] = {}

    def _fake_prepare(raw, *, status, run_async, camilla_factory, verify_only):
        seen["verify_only"] = verify_only
        return SimpleNamespace(
            # The real preparer's own line, so the label this route surfaces is
            # the stage the route asked for rather than one the stub chose.
            label=(
                v2host.V2_CAPTURE_KIND_VERIFY if verify_only
                else v2host.V2_CAPTURE_KIND_SESSION
            ),
            open=lambda *a, **kw: None,
            run_and_consume=lambda *a, **kw: None,
            request_stop=lambda: None,
            position_gate=None,
            request_complete=None,
            request_retake=None,
        )

    def _fake_run_capture(kind, *, idle_hold):
        seen["kind"] = kind
        return {"status": "awaiting_capture"}

    monkeypatch.setattr(correction_capture, "_read_json_body", lambda _h: {})
    monkeypatch.setattr(correction_capture, "_crossover_blocking_phase", lambda: None)
    monkeypatch.setattr(correction_crossover_backend, "status_payload", dict)
    monkeypatch.setattr(v2host, "prepare_v2_session", _fake_prepare)
    monkeypatch.setattr(correction_capture, "_run_capture", _fake_run_capture)

    correction_handlers._handle_crossover_v2_capture(None, verify_only=verify_only)

    assert seen["verify_only"] is verify_only
    assert seen["kind"].label == expected_label



def test_capture_stop_callback_is_atomic_with_starting_state():
    stopped = threading.Event()
    kind = "crossover_sweep:driver"

    correction_capture._set_capture_slot(None)
    try:
        assert correction_capture._begin_capture_slot(
            kind,
            request_stop=stopped.set,
        )
        response = correction_capture._request_capture_stop("crossover_sweep:")
        assert response["status"] == "stopping"
        assert stopped.is_set()
        waiting = correction_capture._publish_capture_waiting(kind)
        assert waiting["status"] == "stopping"
    finally:
        correction_capture._set_capture_slot(None)


def test_capture_failure_message_sanitizes_local_seam_oserror_to_internal_error_copy():
    """W6 hardware run 3 finding G: a bare OSError from the v2 crossover's
    LOCAL play/DSP seam (the DSP writer lock's os.open hitting a read-only
    config_dir, finding F) used to leak the raw errno string —
    "[Errno 30] Read-only file system: '/etc/camilladsp/.dsp_apply.lock'" —
    onto the wizard's capture status line via the generic str(exc) fallback.
    build_v2_run_and_consume wraps it as CrossoverV2LocalSeamError before it
    escapes the seam (see
    tests/test_correction_crossover_v2_endpoints.py::
    test_local_seam_oserror_from_play_maps_to_internal_error); this pins the
    household-facing translation, pulled from the SAME REASON_REGISTRY copy
    the v2 envelope itself renders for internal_error — never the raw
    exception. The raw string still reaches the journal unchanged; only the
    household-facing surface is sanitized here."""
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_INTERNAL_ERROR,
        REASON_REGISTRY,
    )
    from jasper.web.correction_crossover_v2 import CrossoverV2LocalSeamError

    exc = CrossoverV2LocalSeamError(
        "[Errno 30] Read-only file system: '/etc/camilladsp/.dsp_apply.lock'"
    )
    message = correction_capture._capture_failure_message(exc)
    assert message == REASON_REGISTRY[REASON_INTERNAL_ERROR].message
    assert "Errno" not in message
    assert "/etc/camilladsp" not in message


def test_run_async_timeout_waits_for_coroutine_cleanup():
    started = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    finished = threading.Event()
    failures = []

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await asyncio.to_thread(release_cleanup.wait)

    def invoke():
        try:
            correction_capture._run_async(operation(), timeout=0.05)
        except concurrent.futures.TimeoutError:
            pass
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    assert started.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
    assert cleanup_started.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
    assert not finished.is_set()
    release_cleanup.set()
    assert finished.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
    worker.join(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
    assert failures == []


def test_run_async_drain_alarm_keeps_owner_fail_closed(monkeypatch):
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    drain_alarm = threading.Event()
    finished = threading.Event()

    async def operation():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await asyncio.to_thread(release_cleanup.wait)

    monkeypatch.setattr(
        correction_capture,
        "_RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S",
        0.01,
    )
    monkeypatch.setattr(
        correction_capture,
        "log_event",
        lambda _logger, event, **_fields: (
            drain_alarm.set()
            if event == "correction.async_cancel_drain_timeout"
            else None
        ),
    )

    def invoke():
        try:
            correction_capture._run_async(operation(), timeout=0.01)
        except concurrent.futures.TimeoutError:
            pass
        finally:
            finished.set()

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    try:
        assert cleanup_started.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        assert drain_alarm.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        assert not finished.is_set()
    finally:
        release_cleanup.set()
    assert finished.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
    worker.join(timeout=DEFAULT_SIGNAL_TIMEOUT_S)


def test_render_page_delegates_correction_when_bonded_follower(monkeypatch):
    monkeypatch.setattr(correction_setup, "bonded_follower_active", lambda: True)
    leader_paths = []
    monkeypatch.setattr(
        correction_setup,
        "bonded_follower_leader_web_url",
        lambda path="/": leader_paths.append(path) or "http://jts3.local/sound/room/",
    )

    body = correction_setup._render_page("jts4.local", "csrf-token").decode()

    assert "Room correction is controlled by the pair leader" in body
    assert leader_paths == ["/sound/room/"]
    assert "http://jts3.local/sound/room/" in body
    assert "/assets/correction/js/main.js" not in body
    assert 'meta name="jts-csrf" content="csrf-token"' in body


def test_render_page_removes_certificate_install_guide():
    body = correction_setup._render_page("jts.local").decode()
    assert 'href="http://jts.local/jts-root-ca.crt"' not in body
    assert "Certificate Trust Settings" not in body
    assert "browser will warn about the speaker's local certificate" in body


def test_render_page_back_link_returns_to_plain_http():
    """The correction app itself runs under HTTPS, but the rest of the
    JTS wizard surface is deliberately plain HTTP. Its back affordance
    must use an absolute HTTP URL so it does not inherit the HTTPS
    origin and hit nginx's 443 catch-all."""
    body = correction_setup._render_page("jts.local").decode()
    assert 'class="icon-button" href="http://jts.local/sound/"' in body
    assert 'href="/"' not in body


def test_read_json_body_rejects_invalid_content_length():
    class Handler:
        headers = {"Content-Length": "not-a-number"}
        rfile = io.BytesIO()

    with pytest.raises(correction_capture.BadRequest, match="Content-Length"):
        correction_capture._read_json_body(Handler())


def test_local_capture_setup_rejects_a_stale_session_before_binding(monkeypatch):
    from jasper.correction.session import SessionState

    payload = json.dumps({
        "session_id": "old-run",
        "input_device": {"browser_label": "USB mic", "sample_rate": 48000},
    }).encode()
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(payload))},
        rfile=io.BytesIO(payload),
    )
    sess = SimpleNamespace(
        session_id="current-run",
        state=SessionState.NEEDS_NOISE_CAPTURE,
    )
    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)

    with pytest.raises(correction_capture.RequestConflict, match="no longer current"):
        correction_handlers._handle_local_capture_setup(handler)


def test_local_capture_setup_sanitizes_and_binds_current_session(monkeypatch):
    from jasper.correction.session import SessionState

    payload = json.dumps({
        "session_id": "current-run",
        "input_device": {
            "device_id": "browser-secret-id",
            "browser_label": "USB measurement microphone",
            "sample_rate": 48000,
            "channel_count": 1,
            "echo_cancellation": False,
            "noise_suppression": False,
            "auto_gain_control": False,
        },
    }).encode()
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(payload))},
        rfile=io.BytesIO(payload),
    )

    class Session:
        session_id = "current-run"
        state = SessionState.NEEDS_NOISE_CAPTURE
        input_device = None
        mic_calibration = None

        async def bind_local_capture_setup(self, *, mic_calibration, input_device):
            self.mic_calibration = mic_calibration
            self.input_device = input_device
            return {"level": "ok", "failed": False}

    sess = Session()
    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)

    result = correction_handlers._handle_local_capture_setup(handler)

    assert result["state"] == "needs_noise_capture"
    assert result["browser_audio_report"] == {"level": "ok", "failed": False}
    assert sess.input_device["device_id_hash"]
    assert "browser-secret-id" not in str(sess.input_device)


def test_local_noise_upload_rejects_unbound_setup_before_reading_body(monkeypatch):
    from jasper.correction.session import SessionState

    handler = SimpleNamespace(
        headers={"Content-Length": "4"},
        rfile=io.BytesIO(b"WAVE"),
    )
    sess = SimpleNamespace(
        local_capture_setup_bound=False,
        state=SessionState.NEEDS_NOISE_CAPTURE,
    )
    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)

    with pytest.raises(correction_capture.RequestConflict, match="bind the local"):
        correction_handlers._handle_upload_noise(handler)

    assert handler.rfile.tell() == 0


@pytest.mark.parametrize("status", ["idle", "cancelled", "maxed_out", "error"])
def test_local_noise_upload_requires_completed_level_lock_before_body(
    status,
    monkeypatch,
):
    from jasper.correction.session import (
        AutolevelData,
        AutolevelStatus,
        SessionState,
    )

    handler = SimpleNamespace(
        headers={"Content-Length": "4"},
        rfile=io.BytesIO(b"WAVE"),
    )
    sess = SimpleNamespace(
        local_capture_setup_bound=True,
        state=SessionState.NEEDS_NOISE_CAPTURE,
        autolevel=AutolevelData(status=AutolevelStatus(status)),
        autolevel_run_in_progress=False,
    )
    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)

    with pytest.raises(correction_capture.RequestConflict, match="lock the measurement"):
        correction_handlers._handle_upload_noise(handler)

    assert handler.rfile.tell() == 0


def test_local_noise_upload_rearms_watchdog_on_async_loop_before_body(
    tmp_path,
    monkeypatch,
):
    import asyncio
    from jasper.correction.session import (
        AutolevelData,
        AutolevelStatus,
        SessionState,
    )

    events = []

    class Session:
        local_capture_setup_bound = True
        state = SessionState.NEEDS_NOISE_CAPTURE
        current_position = 0
        total_positions = 1
        session_id = "local-run"
        noise_reports = []
        acoustic_quality = None
        autolevel = AutolevelData(status=AutolevelStatus.LOCKED)
        autolevel_run_in_progress = False

        async def resume_capture_timeout_on_loop(self):
            events.append("resume-on-loop")

        def noise_capture_path_for_position(self, _position):
            return tmp_path / "noise.wav"

        async def on_noise_capture_uploaded(self, _path):
            events.append("noise-accepted")

    sess = Session()
    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(
        correction_handlers,
        "_read_wav_body",
        lambda _handler: events.append("body-read") or b"WAVE",
    )
    monkeypatch.setattr(
        correction_capture,
        "_run_async",
        lambda coro, timeout: asyncio.run(coro),
    )
    monkeypatch.setattr(
        correction_capture,
        "_schedule_measurement_sweep",
        lambda *_args, **_kwargs: events.append("sweep-scheduled"),
    )
    monkeypatch.setattr(correction_capture, "_camilla", lambda: object())

    correction_handlers._handle_upload_noise(SimpleNamespace())

    assert events == [
        "resume-on-loop",
        "body-read",
        "noise-accepted",
        "sweep-scheduled",
    ]


def test_local_autolevel_rejects_unbound_setup_before_audio_side_effects(
    monkeypatch,
):
    from jasper.correction.session import SessionState

    sess = SimpleNamespace(
        local_capture_setup_bound=False,
        state=SessionState.NEEDS_NOISE_CAPTURE,
    )
    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)

    with pytest.raises(correction_capture.RequestConflict, match="must be complete"):
        correction_handlers._handle_autolevel_start(SimpleNamespace())


def test_local_autolevel_rejects_stale_restart_after_lock(monkeypatch):
    from jasper.correction.session import (
        AutolevelData,
        AutolevelStatus,
        SessionState,
    )

    sess = SimpleNamespace(
        local_capture_setup_bound=True,
        state=SessionState.NEEDS_NOISE_CAPTURE,
        autolevel=AutolevelData(status=AutolevelStatus.LOCKED),
    )
    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)

    with pytest.raises(correction_capture.RequestConflict, match="already locked"):
        correction_handlers._handle_autolevel_start(SimpleNamespace())


def test_autolevel_start_reserves_run_before_outer_orchestration():
    source = inspect.getsource(correction_handlers._handle_autolevel_start)

    assert source.index("reserve_autolevel_run()") < source.index(
        "asyncio.run_coroutine_threadsafe"
    )
    assert "release_autolevel_run_reservation(reserved)" in source


@pytest.mark.parametrize("prior_status", ["cancelled", "error"])
def test_autolevel_retry_waits_for_a_new_run_identity(prior_status):
    class Data:
        def __init__(self, status):
            self.status = status

        def snapshot(self):
            return {"status": self.status}

    class Future:
        cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    previous = Data(prior_status)
    current = Data("ramping")
    sess = SimpleNamespace(autolevel=previous)
    future = Future()
    timer = threading.Timer(0.05, lambda: setattr(sess, "autolevel", current))
    timer.start()
    try:
        result = correction_handlers._wait_for_new_autolevel_run(
            sess,
            previous,
            future,
            timeout_s=0.5,
        )
    finally:
        timer.cancel()

    assert result == {"status": "ramping"}
    assert future.cancelled is False


def test_read_wav_body_rejects_invalid_content_length():
    class Handler:
        headers = {"Content-Length": "not-a-number"}
        rfile = io.BytesIO()

    with pytest.raises(correction_capture.BadRequest, match="Content-Length"):
        correction_handlers._read_wav_body(Handler())


def test_read_wav_body_rejects_large_or_incomplete_body():
    class TooLarge:
        headers = {"Content-Length": "5"}
        rfile = io.BytesIO(b"12345")

    with pytest.raises(correction_capture.BadRequest, match="too large"):
        correction_handlers._read_wav_body(TooLarge(), max_bytes=4)

    class Incomplete:
        headers = {"Content-Length": "5"}
        rfile = io.BytesIO(b"123")

    with pytest.raises(correction_capture.BadRequest, match="incomplete"):
        correction_handlers._read_wav_body(Incomplete())


def test_render_page_includes_mic_picker_and_calibration_controls():
    # Markup (the picker + model dropdown) stays in the page; the device
    # enumeration + calibration fetch/upload plumbing moved to the module.
    body = correction_setup._render_page("jts.local").decode()
    assert 'id="input-device-select"' in body
    assert 'id="mic-model-select"' in body
    assert "Dayton Audio iMM-6 / iMM-6C" in body
    assert "miniDSP UMIK-1" in body


def test_render_page_includes_browser_audio_path_report():
    body = correction_setup._render_page("jts.local").decode()
    assert 'id="browser-audio-report"' in body  # markup stays in the page


def test_sanitize_input_device_hashes_browser_ids():
    raw = {
        "device_id": "raw-device-id",
        "requested_device_id": "requested-device-id",
        "actual_device_id": "actual-device-id",
        "label": "USB measurement mic",
        "browser_label": "Dayton Audio USB",
        "sample_rate": 48000,
        "source_channel_count": 2,
        "captured_channel_count": 1,
        "echo_cancellation": False,
        "noise_suppression": False,
        "auto_gain_control": False,
        "ignored": "drop me",
    }
    out = correction_capture._sanitize_input_device(raw)
    assert out["label"] == "USB measurement mic"
    assert out["sample_rate"] == 48000.0
    assert out["channel_count"] == 1.0
    assert out["source_channel_count"] == 2.0
    assert out["captured_channel_count"] == 1.0
    assert out["echo_cancellation"] is False
    assert "ignored" not in out
    assert "raw-device-id" not in str(out)
    assert out["device_id_hash"]
    assert out["requested_device_id_hash"]
    assert out["actual_device_id_hash"]


def test_render_page_includes_autolevel_controls():
    """The leveling step is now AUTOMATIC — server ramps main_volume
    while client watches mic and posts /autolevel/lock when in
    target range. Pin the UI presence + JS plumbing.

    Also pins the MANUAL Lock button as a reliable override when the
    auto-detect can't reach the target band (real first-user-test
    finding — speaker-to-iPhone-at-couch path attenuation can leave
    the mic below the lock band even at max safe volume)."""
    body = correction_setup._render_page("jts.local").decode()
    # The envelope owns the sole forward action. Only the in-ramp manual lock
    # and safety cancel stay inside the level section.
    assert 'id="autolevel"' not in body
    assert 'id="autolevel-lock"' in body
    assert 'id="autolevel-cancel"' in body
    assert "Lock now" in body


def test_cancel_measurement_lives_in_always_visible_wizard_chrome():
    body = correction_setup._render_page("jts.local").decode()
    chrome_start = body.index('id="wizard-chrome"')
    chrome_end = body.index("</section>", chrome_start)
    cancel = body.index('id="cancel-measurement"')
    capture_start = body.index('id="position-capture"')
    capture_end = body.index("</section>", capture_start)

    assert chrome_start < cancel < chrome_end
    assert not capture_start < cancel < capture_end
    assert 'id="wizard-chrome" class="wizard-chrome hidden"' not in body


def test_render_page_includes_strategy_without_duplicate_design_audit():
    body = correction_setup._render_page("jts.local").decode()
    assert 'id="strategy-select"' in body  # picker markup stays in the page
    assert "Balanced" in body
    assert "Assertive" not in body
    assert 'id="design-report"' not in body


def test_render_page_keeps_chart_but_removes_duplicate_result_policy():
    body = correction_setup._render_page("jts.local").decode()
    assert 'id="chart"' in body
    assert 'id="chart-show-filter"' in body
    for removed_id in (
        "results-summary",
        "chart-smoothing",
        "chart-show-spread",
        "chart-show-band",
        "confidence-panel",
        "runtime-integrity-panel",
        "design-report",
        "peq-list",
    ):
        assert f'id="{removed_id}"' not in body


def test_render_page_includes_read_only_measurement_reports():
    body = correction_setup._render_page("jts.local").decode()
    # Section containers stay in the page; the report fetch/render/strings
    # moved into the module.
    assert 'data-envelope-section="reports"' in body
    assert 'id="measurement-reports"' not in body
    assert 'id="session-history"' in body
    assert 'id="session-report"' in body


def test_render_page_includes_noise_and_repeat_capture_flow():
    body = correction_setup._render_page("jts.local").decode()
    assert 'id="repeat-main-position"' not in body
    assert 'id="repeat-main-position-disclosure"' in body
    # The presentation envelope owns this copy and fills the initially empty
    # disclosure. test_correction_envelope pins the exact server wording.
    assert "automatically repeats the main-seat measurement once" not in body
    assert 'id="repeat-position"' not in body


def test_render_page_autolevel_requires_ambient_trust_after_tone_start(
    monkeypatch,
):
    """The trust margin the module fails closed without is server-owned."""
    monkeypatch.setenv("JASPER_RAMP_TRUST_MARGIN_DB", "12.5")
    page = correction_setup._render_page("jts.local").decode()
    assert 'data-level-trust-margin-db="12.5"' in page


def test_render_page_placement_advice_says_head_height():
    """First-pass instructions said 'on the seat' which is wrong —
    the cushion absorbs sound and the listener's head is what we
    care about. Pin the corrected wording."""
    body = correction_setup._render_page("jts.local").decode()
    assert "head will be" in body or "head height" in body
    # Negative pin — the bad wording shouldn't come back.
    assert "on the seat" not in body


def test_next_position_is_only_an_envelope_owned_action():
    html = correction_setup._render_page("jts.local").decode()
    assert 'id="continue-position"' not in html


def test_render_page_certificate_copy_is_one_plain_sentence():
    body = correction_setup._render_page("jts.local").decode()
    assert body.count("browser will warn about the speaker's local certificate") == 1
    assert "Optional: silence" not in body
    assert "Profile Downloaded" not in body


# ---------- Test-tone backend (jasper.correction.playback) ------------------


def test_test_tone_wav_is_generated_and_cached(tmp_path):
    """First call generates the WAV; second call reuses the cache
    file (no re-generation). Cache key is the parameter tuple."""
    from jasper.correction import playback
    p1 = playback._ensure_tone_wav(
        freq_hz=1000, duration_s=2.0, dbfs=-18.0,
        sample_rate=48000, cache_dir=tmp_path,
    )
    assert p1.exists()
    mtime1 = p1.stat().st_mtime
    # Second call → same path, cache hit.
    p2 = playback._ensure_tone_wav(
        freq_hz=1000, duration_s=2.0, dbfs=-18.0,
        sample_rate=48000, cache_dir=tmp_path,
    )
    assert p2 == p1
    assert p2.stat().st_mtime == mtime1


def test_test_tone_wav_audio_correctness(tmp_path):
    """The generated WAV should:
      - have the expected duration (within sample-rate resolution)
      - contain a single dominant frequency at the requested freq
      - peak amplitude near the requested dBFS (within fade-edge dip)
    """
    import numpy as np
    from jasper.audio_measurement import sweep
    from jasper.correction import playback

    wav_path = playback._ensure_tone_wav(
        freq_hz=1000, duration_s=1.0, dbfs=-12.0,
        sample_rate=48000, cache_dir=tmp_path,
    )
    sig, sr = sweep.read_wav_mono(wav_path)
    assert sr == 48000
    # Length tolerance: ±10 samples for fade-rounding.
    assert abs(len(sig) - 48000) < 10
    # Peak amplitude target: 10**(-12/20) = 0.251. Allow a bit of
    # margin for fade-edge dip.
    expected_peak = 10 ** (-12.0 / 20)
    actual_peak = float(np.max(np.abs(sig)))
    assert actual_peak <= expected_peak + 0.005
    assert actual_peak > expected_peak * 0.9
    # FFT — the peak bin should be at ~1000 Hz.
    spectrum = np.abs(np.fft.rfft(sig))
    freqs_bin = np.fft.rfftfreq(len(sig), d=1.0 / sr)
    peak_idx = int(np.argmax(spectrum))
    assert abs(freqs_bin[peak_idx] - 1000) < 2  # within 2 Hz


# ---------- End-to-end via the actual HTTP server --------------------------


def _start_server() -> tuple[ThreadingHTTPServer, str]:
    server = correction_setup.make_server(
        ("127.0.0.1", 0), hostname="jts.local",
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def test_e2e_get_index_serves_html():
    server, base = _start_server()
    try:
        resp = urllib.request.urlopen(f"{base}/")
        assert resp.status == 200
        assert resp.headers.get("Content-Type", "").startswith(
            "text/html",
        )
        body = resp.read().decode()
        assert "Room correction" in body
    finally:
        server.shutdown()
        server.server_close()


def test_get_serves_the_speaker_timing_page_on_the_manifest_label():
    """This daemon also serves the page nginx mounts at /sound/pair/sync/:
    manifest label as <title> and header, back to the parent
    (docs/web-ia.md §2). The public path is pinned in
    test_landing_page_html.py; the daemon's own route stays /sync, the
    sibling of /crossover and /bass on this backend."""
    server, base = _start_server()
    try:
        resp = urllib.request.urlopen(f"{base}/sync", timeout=5)
        assert resp.status == 200
        body = resp.read().decode()
    finally:
        server.shutdown()
        server.server_close()

    assert "<title>Speaker timing</title>" in body
    assert '<h1 class="app-header__title">Speaker timing</h1>' in body
    assert 'href="/sound/pair/"' in body
    assert "/assets/sync/sync.css?v=" in body


def test_e2e_bonded_follower_rejects_correction_mutation(monkeypatch):
    monkeypatch.setattr(correction_setup, "bonded_follower_active", lambda: True)
    server, base = _start_server()
    try:
        resp = request_with_csrf(
            base,
            "/apply",
            json.dumps({}).encode("utf-8"),
            content_type="application/json",
            expect_status=409,
        )
        payload = json.loads(resp.read().decode("utf-8"))
        assert "controlled on the pair leader" in payload["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_start_safety_refusal_returns_422(monkeypatch):
    from jasper.correction.runtime_safety import CorrectionRuntimeSafetyError

    def fake_start(handler):
        raise CorrectionRuntimeSafetyError("flat sweep is unsafe")

    monkeypatch.setattr(correction_handlers, "_handle_start", fake_start)
    server, base = _start_server()
    try:
        e = request_with_csrf(
            base,
            "/start",
            b"{}",
            content_type="application/json",
            expect_status=422,
        )
        body = json.loads(e.read().decode())
        assert body == {
            "failure": {
                "code": "speaker_measurement_unsafe",
                "text": (
                    "The speaker is not ready to measure safely. Review "
                    "speaker setup, then try again."
                ),
                "retryable": False,
                # The reachable cause of this refusal is now an unready
                # speaker — `/start` no longer turns that away before the
                # graph load — so the household gets somewhere to act rather
                # than a retry that would refuse again.
                "recovery_action": {
                    "label": "Open speaker setup",
                    "href": "/sound/setup/",
                },
            },
        }
        assert "flat sweep is unsafe" not in str(body)
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_apply_reaches_the_dsp_despite_failed_measurement_evidence(monkeypatch):
    """The nanny burn-down at ``/apply`` — doctrine deviation (d).

    This used to assert a 422 with ``measurement_evidence_unsafe``, raised
    before ``_camilla()`` was ever reached. That blocker refused a reversible,
    measurable experiment on a judgement about how good the evidence was, and
    no cause it fires on — a failed capture-quality, browser-audio-path, or
    runtime-integrity check — is on the doctrine's closed hard-stop list. The
    doubt now reaches the household as a ``warn`` nudge on the envelope
    (``test_correction_envelope``) and the apply proceeds to the graph.

    **Mutation guard.** Restoring the pre-check makes ``_camilla`` unreachable
    and fails the ``reached`` assertion.
    """
    sess = SimpleNamespace(
        confidence_report={
            "findings": [{
                "code": "runtime_integrity_failed",
                "severity": "fail",
                "message": "raw runtime diagnostic",
            }],
        },
    )
    reached: list[str] = []

    def _reached_dsp():
        reached.append("camilla")
        raise RuntimeError("stop here — the DSP itself is not this test's subject")

    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(correction_capture, "_camilla", _reached_dsp)
    server, base = _start_server()
    try:
        request_with_csrf(
            base,
            "/apply",
            b"{}",
            content_type="application/json",
            expect_status=500,
        )
        assert reached == ["camilla"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("route", ["/interpret", "/propose"])
def test_e2e_spend_cap_exceeded_returns_429_with_honest_json(monkeypatch, route):
    """The spend-cap refusal maps to HTTP 429 (distinct from RequestConflict's
    409) with the rollover-worded JSON body the panel renders verbatim. Drives
    the real do_POST dispatch for both paid routes."""
    handler_name = (
        "_handle_interpret" if route == "/interpret" else "_handle_propose"
    )

    def fake(handler):
        raise correction_tuning.SpendCapExceeded(
            "daily spend cap reached — the tuning assistant will be "
            "available again after the daily rollover"
        )

    monkeypatch.setattr(correction_handlers, handler_name, fake)
    server, base = _start_server()
    try:
        e = request_with_csrf(
            base,
            route,
            b"{}",
            content_type="application/json",
            expect_status=429,
        )
        body = json.loads(e.read().decode())
        assert body == {
            "failure": {
                "code": "tuning_spend_limit",
                "text": (
                    "The daily assistant budget is reached. Try again after "
                    "the daily rollover."
                ),
                "retryable": False,
                "recovery_action": None,
            },
        }
        assert "daily spend cap reached" not in str(body)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("route", "advisor_name"),
    [("/interpret", "interpret"), ("/propose", "propose")],
)
def test_e2e_tuning_provider_error_returns_closed_400(
    monkeypatch,
    tmp_path,
    route,
    advisor_name,
):
    """Real provider diagnostics cross both backend exception boundaries but
    never escape the closed Room failure catalog."""
    from jasper.calibration_agent import correction_advisor, model_client

    monkeypatch.setenv("JASPER_USAGE_DB", str(tmp_path / "usage.db"))
    monkeypatch.setenv(
        "JASPER_VOICE_PROVIDER_FILE",
        str(tmp_path / "voice_provider.env"),
    )
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_USD", "0")
    monkeypatch.setattr(
        "jasper.calibration_agent.key_provisioning.tuning_llm_available",
        lambda **_: True,
    )
    session = object()
    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: session)
    correction_tuning._tuning_last_paid_call[0] = 0.0

    def fail_provider(called_session, **_kwargs):
        assert called_session is session
        raise model_client.AdvisorModelError("raw provider diagnostic")

    monkeypatch.setattr(correction_advisor, advisor_name, fail_provider)
    server, base = _start_server()
    try:
        error = request_with_csrf(
            base,
            route,
            b"{}",
            content_type="application/json",
            expect_status=400,
        )
        body = json.loads(error.read().decode())
        assert body == {
            "failure": {
                "code": "tuning_request_failed",
                "text": "The tuning assistant could not continue. Try again.",
                "retryable": True,
                "recovery_action": None,
            },
        }
        assert "raw provider diagnostic" not in str(body)
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_healthz_returns_plain_ok():
    """systemd's `Type=notify` could replace this later, but for now a
    simple HTTP-200 / "ok" body is what makes `curl jts.local/sound/room/healthz`
    a valid liveness probe — and also lets jasper-doctor add a
    correction-subsystem check without parsing JSON."""
    server, base = _start_server()
    try:
        resp = urllib.request.urlopen(f"{base}/healthz")
        assert resp.status == 200
        assert resp.headers.get("Content-Type", "").startswith(
            "text/plain",
        )
        assert resp.read() == b"ok\n"
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_unknown_path_404s():
    server, base = _start_server()
    try:
        try:
            urllib.request.urlopen(f"{base}/nope")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        else:
            raise AssertionError("expected 404 for unknown path")
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_calibration_upload_parses_and_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path))
    server, base = _start_server()
    try:
        payload = json.dumps({
            "filename": "lab.txt",
            "content": "20 -1\n100 0\n1000 1\n",
            "model": "other",
            "label": "Lab mic",
            "sign_convention": "correction",
        }).encode()
        resp = request_with_csrf(
            base,
            "/calibration/upload",
            payload,
            content_type="application/json",
        )
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["calibration"]["provider"] == "manual_upload"
        assert data["calibration"]["point_count"] == 3
        assert data["calibration"]["calibration_id"]
        assert data["preview"]["freqs_hz"][0] == 20.0
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_calibration_upload_defaults_to_the_response_convention(
    tmp_path, monkeypatch,
):
    """An upload that declares no convention is read as the mic's RESPONSE.

    That is what a measurement-mic calibration file states (the page's own
    control and help copy say so), so an omitted field must resolve to the
    same answer the household would have picked, not the opposite one.
    """
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path))
    server, base = _start_server()
    try:
        payload = json.dumps({
            "filename": "lab.txt",
            "content": "20 -1\n100 0\n1000 1\n",
            "model": "other",
            "label": "Lab mic",
        }).encode()
        resp = request_with_csrf(
            base,
            "/calibration/upload",
            payload,
            content_type="application/json",
        )
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["calibration"]["sign_convention"] == "response"
        # The mic reads 1 dB LOW at 20 Hz and 1 dB HIGH at 1 kHz, so the
        # correction adds 1 dB and cuts 1 dB respectively.
        assert data["preview"]["correction_db"] == [1.0, 0.0, -1.0]
    finally:
        server.shutdown()
        server.server_close()


def test_calibration_upload_card_defaults_to_the_response_convention():
    """The page's own control agrees with the endpoint default."""
    sign_select = (
        correction_setup._PAGE_BODY
        .split('id="calibration-sign"', 1)[1]
        .split("</select>", 1)[0]
    )
    assert '<option value="response" selected>' in sign_select
    assert "selected" not in sign_select.split('value="correction"', 1)[1]


def test_e2e_calibration_upload_bad_file_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path))
    server, base = _start_server()
    try:
        payload = json.dumps({
            "filename": "bad.txt",
            "content": "this is not a calibration file",
            "model": "other",
            "label": "Lab mic",
        }).encode()
        e = request_with_csrf(
            base,
            "/calibration/upload",
            payload,
            content_type="application/json",
            expect_status=400,
        )
        body = json.loads(e.read().decode())
        assert "at least 2 rows" in body["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_invalid_json_returns_400():
    server, base = _start_server()
    try:
        e = request_with_csrf(
            base,
            "/calibration/upload",
            b"{not json",
            content_type="application/json",
            expect_status=400,
        )
        body = json.loads(e.read().decode())
        assert "invalid JSON" in body["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_calibration_fetch_upstream_failure_returns_502(monkeypatch):
    from jasper.audio_measurement import calibration

    def fake_fetch_vendor_calibration(**kwargs):
        raise calibration.CalibrationUpstreamError("miniDSP unavailable")

    monkeypatch.setattr(
        calibration,
        "fetch_vendor_calibration",
        fake_fetch_vendor_calibration,
    )
    server, base = _start_server()
    try:
        payload = json.dumps({
            "model": "minidsp_umik2",
            "serial": "810-8494",
        }).encode()
        e = request_with_csrf(
            base,
            "/calibration/fetch",
            payload,
            content_type="application/json",
            expect_status=502,
        )
        body = json.loads(e.read().decode())
        assert body["error"] == "miniDSP unavailable"
    finally:
        server.shutdown()
        server.server_close()


def _stored_umik2(tmp_path, *, serial="810-8494"):
    """Establish a UMIK-2 calibration and remember it as the household mic."""
    from jasper.audio_measurement import calibration
    from jasper.correction.household_mic import (
        household_mic_from_calibration,
        write_household_mic,
    )

    record = calibration.store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="minidsp",
        model="minidsp_umik2",
        label="miniDSP UMIK-2",
        source="https://vendor.example/cal.txt",
        serial=serial,
        root=tmp_path / "cal",
    )
    write_household_mic(
        household_mic_from_calibration(record, serial=serial),
        path=tmp_path / "household_mic.json",
    )
    return record


def _setup_reference(record, *, model="minidsp_umik2"):
    """The reference shape the measurement source mints from the record."""
    return {
        "calibration": {
            "mode": "stored",
            "calibration_id": record.calibration_id,
            "model": model,
        },
    }


def test_household_mic_replaced_on_a_different_model(tmp_path, monkeypatch, caplog):
    """A different mic is never refused: the new success replaces the record
    and says so with the model pair."""
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))
    caplog.set_level(logging.INFO, logger="jasper.web.correction_setup")

    from jasper.audio_measurement import calibration
    from jasper.correction.household_mic import read_household_mic

    first = calibration.store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        root=tmp_path / "cal",
    )
    correction_capture._save_household_mic(first)
    caplog.clear()

    second = calibration.store_calibration(
        text="20 -2\n100 0\n1000 2\n",
        provider="manual_upload",
        model="dayton_imm6",
        label="New lab mic",
        source="uploaded:lab2.txt",
        root=tmp_path / "cal",
    )
    correction_capture._save_household_mic(second)

    record = read_household_mic(path=household_path)
    assert record is not None
    assert record.model_key == "dayton_imm6"  # replaced, not merged or refused
    assert "event=correction.household_mic_replaced" in caplog.text
    assert "old_model=other" in caplog.text
    assert "new_model=dayton_imm6" in caplog.text


def test_household_mic_replaced_on_a_different_serial(tmp_path, monkeypatch, caplog):
    """Within one model, a different physical unit (serial_hash) is still a
    mic swap: the record is replaced and household_mic_replaced fires with a
    `changed=serial` discriminator — while the serial hashes themselves stay
    out of the log line."""
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))
    caplog.set_level(logging.INFO, logger="jasper.web.correction_setup")

    from jasper.audio_measurement import calibration
    from jasper.audio_measurement.calibration import serial_hash
    from jasper.correction.household_mic import read_household_mic

    for serial in ("810-1111", "810-2222"):
        record = calibration.store_calibration(
            text=f"20 -1\n100 0\n1000 1\n# unit {serial}\n",
            provider="minidsp",
            model="minidsp_umik2",
            label="miniDSP UMIK-2",
            source="https://vendor.example/cal.txt",
            serial=serial,
            root=tmp_path / "cal",
        )
        correction_capture._save_household_mic(record, serial=serial)

    stored = read_household_mic(path=household_path)
    assert stored is not None
    assert stored.serial_hash == serial_hash("810-2222")
    assert "event=correction.household_mic_replaced" in caplog.text
    assert "changed=serial" in caplog.text
    # Hashes never ride the event line.
    assert serial_hash("810-1111") not in caplog.text
    assert serial_hash("810-2222") not in caplog.text


def test_household_mic_write_failure_never_blocks_the_calibration(
    tmp_path, monkeypatch, caplog,
):
    """The documented never-block invariant: persisting the household record
    is best-effort. A write failure logs one WARN and the caller continues."""
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))
    caplog.set_level(logging.WARNING, logger="jasper.web.correction_setup")

    from jasper.audio_measurement import calibration
    from jasper.correction import household_mic

    def boom(record, *, path):
        raise OSError("disk full")

    monkeypatch.setattr(household_mic, "write_household_mic", boom)

    record = calibration.store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        root=tmp_path / "cal",
    )
    correction_capture._save_household_mic(record)

    assert not household_path.exists()
    assert "failed to persist household mic record" in caplog.text


def test_setup_reference_resolves_the_remembered_calibration(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "household_mic.json"),
    )
    from jasper.web import correction_crossover_v2 as v2host

    record = _stored_umik2(tmp_path)
    resolved = v2host.resolve_setup_calibration(_setup_reference(record), None)
    assert resolved is not None
    assert resolved.calibration_id == record.calibration_id


def test_setup_reference_resolves_an_uploaded_calibration(tmp_path, monkeypatch):
    """An upload-provenance record resolves identically: the reference names a
    calibration_id, not how the household established it."""
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "household_mic.json"),
    )
    from jasper.audio_measurement import calibration
    from jasper.correction.household_mic import (
        household_mic_from_calibration,
        write_household_mic,
    )
    from jasper.web import correction_crossover_v2 as v2host

    record = calibration.store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        root=tmp_path / "cal",
    )
    write_household_mic(
        household_mic_from_calibration(record),
        path=tmp_path / "household_mic.json",
    )
    resolved = v2host.resolve_setup_calibration(
        _setup_reference(record, model="other"), None,
    )
    assert resolved is not None
    assert resolved.calibration_id == record.calibration_id


@pytest.mark.parametrize(
    ("device", "expect_applied"),
    (
        ({"label": "iMM-6C"}, False),          # a DIFFERENT registered model
        ({"label": "UMIK-2 (2752:002b)"}, True),
        ({"label": "Some Unbranded Capture"}, True),  # nothing to contradict
        ({}, True),                            # no label reported
        (None, True),                          # no device offered at all
    ),
)
def test_setup_reference_refuses_a_different_mic(
    tmp_path, monkeypatch, device, expect_applied,
):
    """The 2026-07-20 incident: the reference names the household's UMIK-2 but
    THIS capture reports a Dayton iMM-6C. Refusing answers None, so the
    caller's uncalibrated-analysis path takes over — never a blocked capture,
    and never a re-persisted wrong pairing."""
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))
    from jasper.web import correction_crossover_v2 as v2host

    record = _stored_umik2(tmp_path)
    before = household_path.read_text()

    resolved = v2host.resolve_setup_calibration(_setup_reference(record), device)

    if expect_applied:
        assert resolved is not None
        assert resolved.calibration_id == record.calibration_id
    else:
        assert resolved is None
    assert household_path.read_text() == before  # never re-persisted either way


def test_setup_reference_mismatch_is_journalled(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "household_mic.json"),
    )
    caplog.set_level(logging.WARNING, logger="jasper.correction.household_mic")
    from jasper.web import correction_crossover_v2 as v2host

    record = _stored_umik2(tmp_path)
    v2host.resolve_setup_calibration(
        _setup_reference(record), {"label": "iMM-6C", "device_id": "dayton"},
    )
    assert "event=correction.calibration_device_identity_mismatch" in caplog.text
    assert "stored_model=minidsp_umik2" in caplog.text


def test_setup_reference_without_a_calibration_resolves_to_nothing(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "household_mic.json"),
    )
    from jasper.web import correction_crossover_v2 as v2host

    assert v2host.resolve_setup_calibration(None, None) is None
    assert v2host.resolve_setup_calibration({}, None) is None
    assert v2host.resolve_setup_calibration({"calibration": {"mode": "none"}}, None) \
        is None


def test_a_stale_setup_reference_is_a_named_rejection(tmp_path, monkeypatch):
    """A reference to a calibration that is no longer on disk raises loudly
    with household-facing copy, rather than silently measuring uncalibrated."""
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))
    from jasper.web import correction_crossover_v2 as v2host

    with pytest.raises(ValueError, match="no longer available"):
        v2host.resolve_setup_calibration(
            {
                "calibration": {
                    "mode": "stored",
                    "calibration_id": "does-not-exist",
                    "model": "minidsp_umik2",
                },
            },
            None,
        )
    assert not household_path.exists()  # no write on a resolution miss


def test_a_setup_reference_without_an_id_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "household_mic.json"),
    )
    from jasper.web import correction_crossover_v2 as v2host

    with pytest.raises(ValueError, match="calibration_id is required"):
        v2host.resolve_setup_calibration(
            {"calibration": {"mode": "stored", "model": "minidsp_umik2"}}, None,
        )



def test_e2e_calibration_fetch_success_saves_household_mic(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    from jasper.audio_measurement import calibration

    def fake_fetch_vendor_calibration(
        *, model_key, serial, orientation, root, opener=None,
    ):
        return calibration.store_calibration(
            text="20 -1\n100 0\n1000 1\n",
            provider="dayton_audio",
            model=model_key,
            label="Dayton Audio iMM-6 / iMM-6C",
            source="https://vendor.example/cal.txt",
            serial=serial,
            orientation=orientation,
            root=root,
        )

    monkeypatch.setattr(
        calibration, "fetch_vendor_calibration", fake_fetch_vendor_calibration,
    )

    server, base = _start_server()
    try:
        payload = json.dumps({
            "model": "dayton_imm6",
            "serial": "700-1234",
            "orientation": "0deg",
        }).encode()
        resp = request_with_csrf(
            base,
            "/calibration/fetch",
            payload,
            content_type="application/json",
        )
        assert resp.status == 200
    finally:
        server.shutdown()
        server.server_close()

    from jasper.correction.household_mic import read_household_mic

    record = read_household_mic(path=household_path)
    assert record is not None
    assert record.model_key == "dayton_imm6"
    assert record.provider == "dayton_audio"
    assert record.serial_display == "1234"


def test_e2e_calibration_upload_success_saves_household_mic(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    server, base = _start_server()
    try:
        payload = json.dumps({
            "filename": "lab.txt",
            "content": "20 -1\n100 0\n1000 1\n",
            "model": "other",
            "label": "Lab mic",
            "sign_convention": "correction",
        }).encode()
        resp = request_with_csrf(
            base,
            "/calibration/upload",
            payload,
            content_type="application/json",
        )
        assert resp.status == 200
    finally:
        server.shutdown()
        server.server_close()

    from jasper.correction.household_mic import read_household_mic

    record = read_household_mic(path=household_path)
    assert record is not None
    assert record.model_key == "other"
    assert record.provider == "manual_upload"
    assert record.serial_display is None  # uploads never carry a serial


def test_default_setup_calibration_for_spec_present_and_absent(tmp_path, monkeypatch):
    cal_root = tmp_path / "cal"
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(cal_root))
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    assert correction_capture._default_setup_calibration_for_spec() is None

    from jasper.audio_measurement.calibration import store_calibration
    from jasper.correction.household_mic import (
        household_mic_from_calibration,
        write_household_mic,
    )

    record = store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="minidsp",
        model="minidsp_umik2",
        label="miniDSP UMIK-2",
        source="https://vendor.example/cal.txt",
        serial="810-8494",
        root=cal_root,
    )
    write_household_mic(
        household_mic_from_calibration(record, serial="810-8494"),
        path=household_path,
    )

    hint = correction_capture._default_setup_calibration_for_spec()
    assert hint is not None
    assert hint.mode == "serial"
    assert hint.model == "minidsp_umik2"
    assert hint.serial_display == "8494"
    assert hint.calibration_id == record.calibration_id
    # A record that resolves cleanly gates the phone page's one-tap "stored"
    # confirm (a separate capture-page PR) on this flag.
    assert hint.resolvable is True


def test_default_setup_calibration_for_spec_resolvable_is_a_fresh_check(
    tmp_path, monkeypatch,
):
    """`resolvable` is deliberately a SECOND, independent resolver call, not
    inferred from `_resolved_household_mic()` having just succeeded — so a
    resolver hiccup between the two calls degrades to "no one-tap" (the hint
    still ships, just without `resolvable`) instead of dropping the whole
    hint or raising."""
    cal_root = tmp_path / "cal"
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(cal_root))
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    from jasper.audio_measurement.calibration import store_calibration
    from jasper.correction import household_mic
    from jasper.correction.household_mic import (
        household_mic_from_calibration,
        resolve_household_mic_calibration,
        write_household_mic,
    )

    record = store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="minidsp",
        model="minidsp_umik2",
        label="miniDSP UMIK-2",
        source="https://vendor.example/cal.txt",
        serial="810-8494",
        root=cal_root,
    )
    write_household_mic(
        household_mic_from_calibration(record, serial="810-8494"),
        path=household_path,
    )

    calls = []

    def flaky_resolve(household, *, root=None):
        calls.append(household)
        # First call is `_resolved_household_mic()` building the hint's other
        # fields; second is the dedicated `resolvable` check.
        if len(calls) == 1:
            return resolve_household_mic_calibration(household, root=root)
        return None

    monkeypatch.setattr(household_mic, "resolve_household_mic_calibration", flaky_resolve)

    hint = correction_capture._default_setup_calibration_for_spec()
    assert hint is not None  # the hint itself still ships
    assert hint.calibration_id == record.calibration_id
    assert hint.resolvable is False  # but the one-tap confirm is not offered
    assert len(calls) == 2


def test_render_page_omits_household_mic_island_data_when_absent(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "household_mic.json"),
    )
    body = correction_setup._render_page("acoustic-lab.local").decode()
    assert '<script type="application/json" id="household-mic-data">null</script>' in body


def test_render_page_prefills_household_mic_when_record_exists(tmp_path, monkeypatch):
    cal_root = tmp_path / "cal"
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(cal_root))
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    from jasper.audio_measurement.calibration import store_calibration
    from jasper.correction.household_mic import (
        household_mic_from_calibration,
        write_household_mic,
    )

    record = store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="manual_upload",
        model="other",
        label="Living Room UMIK",
        source="uploaded:umik.txt",
        root=cal_root,
    )
    write_household_mic(household_mic_from_calibration(record), path=household_path)

    body = correction_setup._render_page("acoustic-lab.local").decode()
    assert "Living Room UMIK" in body
    assert record.calibration_id in body
    assert 'id="household-mic-banner"' in body
    assert 'id="household-mic-data"' in body


def test_e2e_upload_quality_failure_returns_422(tmp_path, monkeypatch):
    from jasper.audio_measurement import quality
    from jasper.correction.session import SessionState

    report = quality.CaptureQuality(
        sample_rate=48000,
        duration_s=1.0,
        peak_dbfs=0.0,
        rms_dbfs=-3.0,
        clipped_fraction=0.1,
        issues=(
            quality.QualityIssue(
                code="capture_clipped",
                severity="fail",
                message="capture clipped; lower speaker volume and re-measure",
            ),
        ),
    )
    report_dict = report.to_dict()
    report_dict["capture_kind"] = "measurement"
    report_dict["position_index"] = 0
    report_dict["artifact_path"] = "captures/p0.wav"

    class FakeSession:
        session_id = "quality-fail"
        state = SessionState.AWAITING_CAPTURE
        current_position = 0
        total_positions = 1
        capture_quality: list[dict] = []
        verify_quality = None
        measured_curve = None
        target_curve = None
        predicted_curve = None
        verify_curve = None
        verify_metrics = None
        peqs = []
        design_report = None
        confidence_report = None

        def capture_path_for_position(self, position: int):
            return tmp_path / f"p{position}.wav"

        async def on_capture_uploaded(self, path):
            self.state = SessionState.FAILED
            self.capture_quality = [report_dict]
            raise quality.CaptureQualityError(report)

    fake = FakeSession()
    monkeypatch.setattr(
        correction_capture, "_get_or_create_session", lambda: fake,
    )

    server, base = _start_server()
    try:
        e = request_with_csrf(
            base,
            "/upload-capture",
            b"not really a wav",
            content_type="audio/wav",
            expect_status=422,
        )
        body = json.loads(e.read().decode())
        assert "capture quality failed" in body["error"]
        assert body["state"] == "failed"
        assert body["capture_quality"][0]["capture_kind"] == "measurement"
        assert body["capture_quality"][0]["issues"][0]["code"] == (
            "capture_clipped"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_correction_posts_require_csrf():
    server, base = _start_server()
    try:
        req = urllib.request.Request(
            f"{base}/calibration/upload",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            assert e.code == 403
        else:
            raise AssertionError("expected HTTP 403")
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_local_setup_and_noise_conflicts_are_client_errors(monkeypatch):
    def missing_calibration(_handler):
        raise FileNotFoundError("unknown microphone calibration")

    def unbound_noise(_handler):
        raise correction_capture.RequestConflict("bind the local microphone first")

    def unbound_level(_handler):
        raise correction_capture.RequestConflict("bind before level matching")

    monkeypatch.setattr(
        correction_handlers,
        "_handle_local_capture_setup",
        missing_calibration,
    )
    monkeypatch.setattr(
        correction_handlers,
        "_handle_upload_noise",
        unbound_noise,
    )
    monkeypatch.setattr(
        correction_handlers,
        "_handle_autolevel_start",
        unbound_level,
    )
    server, base = _start_server()
    try:
        request_with_csrf(
            base,
            "/local-capture/setup",
            b"{}",
            content_type="application/json",
            expect_status=400,
        )
        request_with_csrf(
            base,
            "/upload-noise",
            b"WAVE",
            content_type="audio/wav",
            expect_status=409,
        )
        request_with_csrf(
            base,
            "/autolevel/start",
            b"{}",
            content_type="application/json",
            expect_status=409,
        )
    finally:
        server.shutdown()
        server.server_close()


def test_sync_analyze_rejects_oversized_capture_before_body_read():
    handler_cls = correction_setup._make_handler_class(
        hostname="jts.local", idle_hold=nullcontext,
    )
    handler = handler_cls.__new__(handler_cls)
    handler.headers = Message()
    handler.headers["Content-Length"] = str(2 * 1024 * 1024 + 1)
    handler.rfile = io.BytesIO(b"")
    sent: dict = {}

    def _send_json(payload, status=200):
        sent["payload"] = payload
        sent["status"] = int(status)

    handler._send_json = _send_json

    handler._dispatch_sync("/sync/analyze")

    assert sent["status"] == 400
    assert "WAV body too large" in sent["payload"]["error"]


def test_e2e_trailing_slash_index_serves_same_html():
    """Defensive: nginx strips its /sound/room/ prefix and forwards as
    GET / — but a future client (curl, jasper-doctor, integration test)
    might hit GET // or GET /. Both should serve the page."""
    server, base = _start_server()
    try:
        for path in ("/", ""):
            resp = urllib.request.urlopen(f"{base}{path}")
            assert resp.status == 200
            assert b"Room correction" in resp.read()
    finally:
        server.shutdown()
        server.server_close()


# --- Bug 1 regression: calibration↔device mismatch backstop -----------------
# A vendor measurement-mic calibration applied to phone-built-in-mic audio
# silently invalidates the measurement. The browser blocks it, but this
# server-side gate is the reliable backstop. Reproduces the cmm31555 iMM-6C
# run on 2026-06-04 where input_device.browser_label was "iPhone Microphone".
import types  # noqa: E402


def _cal(provider):
    return types.SimpleNamespace(provider=provider)


def test_calibration_device_mismatch_blocks_vendor_mic_on_builtin():
    for label in ("iPhone Microphone", "iPad Microphone", "MacBook Pro Microphone",
                  "Built-in Microphone", "Default"):
        msg = correction_capture._calibration_device_mismatch(
            _cal("dayton_audio"), {"browser_label": label}
        )
        assert msg is not None, label
        assert "USB" in msg
    # miniDSP is also an external-only provider
    assert correction_capture._calibration_device_mismatch(
        _cal("minidsp"), {"browser_label": "iPhone Microphone"}
    ) is not None


def test_calibration_device_mismatch_allows_real_usb_mic():
    for label in ("iMM-6C", "USB Audio Device", "UMIK-1", "Microphone 2"):
        assert correction_capture._calibration_device_mismatch(
            _cal("dayton_audio"), {"browser_label": label}
        ) is None, label


def test_calibration_device_mismatch_ignores_manual_and_absent():
    # Manual "other" upload: we can't assume it isn't a phone curve — don't gate.
    assert correction_capture._calibration_device_mismatch(
        _cal("other"), {"browser_label": "iPhone Microphone"}
    ) is None
    # No calibration / no device → nothing to check.
    assert correction_capture._calibration_device_mismatch(
        None, {"browser_label": "iPhone Microphone"}
    ) is None
    assert correction_capture._calibration_device_mismatch(
        _cal("dayton_audio"), None
    ) is None


# --- 2026-07-20 incident: stored calibration must not cross mic identities --
# A Dayton iMM-6C capture ran with the STORED UMIK-2 calibration silently
# applied (setup.calibration.mode="stored" re-confirms whatever
# calibration_id the phone echoes, independent of which mic actually
# recorded). This is `_calibration_device_mismatch`'s sibling: that gate
# catches "vendor curve on the phone's OWN built-in mic"; this one catches
# "vendor curve for a DIFFERENT external measurement mic than reported".


def test_render_page_emits_registry_model_aliases():
    # Inference is registry-driven: each model option carries data-aliases
    # from SUPPORTED_MODELS so the frontend has no hardcoded mic map to drift.
    body = correction_setup._render_page("jts.local").decode()
    assert 'value="dayton_imm6"' in body
    assert 'data-aliases="iMM-6"' in body
    assert 'data-aliases="umik-2"' in body


# --- Audio-safety regression: autolevel volume must be restored even when ----
# apply()/reset() raises. Autolevel ramps main_volume well above the listening
# level for measurement SNR; if a failed apply/reset skipped the restore, the
# next song would play back at the (loud) measurement level. The restore now
# lives in a finally so the exception can't strand the speaker loud.
def _locked_autolevel_session(
    raises_on,
    *,
    original=-20.0,
    config_dir: Path | None = None,
):
    """Fake session whose apply/reset raises, with a LOCKED autolevel that
    ramped main_volume up to a measurement level above `original`."""
    from jasper.correction.session import AutolevelData, AutolevelStatus, SessionState

    class _FakeSession:
        session_id = "vol-strand"
        state = SessionState.READY
        config_path = None
        room_authority_binding = (False, "passive_not_required", None)

        def __init__(self):
            if config_dir is not None:
                self.cfg = SimpleNamespace(config_dir=config_dir)
            self.autolevel = AutolevelData(
                status=AutolevelStatus.LOCKED,
                original_main_volume_db=original,
                locked_main_volume_db=-8.0,
            )

        async def _restore_listening_volume_if_ramped(self):
            from jasper.correction.autolevel import AutolevelController

            controller = AutolevelController(session_id=self.session_id)
            controller.data = self.autolevel
            controller.main_volume_setter = correction_capture._household_level_door()
            return await controller.restore_listening_volume_if_ramped()

        async def apply(
            self,
            set_cb,
            camilla_get_config=None,
            *,
            prepare_guard=None,
        ):
            if prepare_guard is not None:
                await prepare_guard()
            if raises_on == "apply":
                raise RuntimeError("CamillaDSP reload failed")

        async def reset(self, set_cb, **kwargs):
            if raises_on == "reset":
                raise RuntimeError("reset reload failed")

    return _FakeSession()


def _volume_recording_cam(restored):
    class _FakeCam:
        async def set_config_file_path(self, path, best_effort=False):
            return True

        async def get_config_file_path(self, best_effort=True):
            return None

        async def set_volume_db(self, db, best_effort=False):
            restored.append(db)

    return _FakeCam()


def _install_recording_volume_owner(written):
    """Install a process fader owner whose door records what it writes.

    The autolevel restore DECLARES its level through the owner rather than
    writing the fader, so a test that watches the Camilla double sees nothing.
    `tests/conftest.py`'s `_isolate_process_volume_owner` clears the
    registration after each test, so this needs no teardown of its own.
    """
    from jasper.volume_owner import VolumeOwner, install_volume_owner

    live = {"db": 0.0}

    async def _set(db: float) -> bool:
        live["db"] = float(db)
        written.append(float(db))
        return True

    async def _get() -> float:
        return live["db"]

    owner = VolumeOwner(set_fader_db=_set, get_fader_db=_get)
    install_volume_owner(owner)
    return owner


async def test_ready_reset_restores_exact_pre_measurement_graph():
    from jasper.correction.session import SessionState

    predecessor = Path("/var/lib/camilladsp/configs/before-room.yml")
    restore = Path(
        "/var/lib/camilladsp/configs/sound_snapshot_smoke_123.yml"
    )
    measurement = Path(
        "/var/lib/camilladsp/configs/correction_measurement_smoke.yml"
    )
    sess = SimpleNamespace(
        state=SessionState.READY,
        pre_measurement_config_path=predecessor,
        pre_measurement_restore_path=restore,
        measurement_config_path=measurement,
    )

    class Cam:
        async def get_config_file_path(self, *, best_effort=False):
            return str(measurement)

    assert (
        await correction_handlers._resolve_reset_target_async(sess, Cam())
        == restore
    )


@pytest.mark.parametrize("automatic", [False, True], ids=["reset", "auto-revert"])
@pytest.mark.parametrize(
    "running_path",
    ["measurement", "predecessor"],
    ids=["blocked-active-build", "failed-active-load-rollback"],
)
async def test_room_reversal_uses_immutable_running_graph_after_active_overwrite(
    tmp_path,
    automatic,
    running_path,
):
    """A mutable Active candidate filename is provenance, never rollback data."""
    from jasper.correction.session import SessionState

    predecessor = tmp_path / "active_speaker_manual_current.yml"
    restore = tmp_path / "sound_snapshot_roomrun_123.yml"
    measurement = tmp_path / "correction_measurement_roomrun_123.yml"
    running_graph = "# Source: old-active\nfilters:\n  crossover: {}\n"
    refused_candidate = "# Source: refused-active\nfilters:\n  crossover_new: {}\n"
    restore.write_text(running_graph, encoding="utf-8")
    measurement.write_text("filters: {}\n", encoding="utf-8")
    # Active's candidate builder legally rewrote its durable filename, but the
    # candidate was blocked or failed and these bytes never became the graph
    # CamillaDSP was running.
    predecessor.write_text(refused_candidate, encoding="utf-8")
    loaded = []

    class Cam:
        current = str(
            measurement if running_path == "measurement" else predecessor
        )

        async def get_config_file_path(self, *, best_effort=False):
            return self.current

        async def get_active_config_raw(self, *, best_effort=False):
            return running_graph

        async def set_config_file_path(self, path, *, best_effort=False):
            loaded.append(path)
            self.current = path
            return True

    class Session:
        session_id = "roomrun"
        state = SessionState.FAILED
        pre_measurement_config_path = predecessor
        pre_measurement_restore_path = restore
        measurement_config_path = measurement
        cfg = SimpleNamespace(
            config_dir=tmp_path,
            base_config_path=tmp_path / "base.yml",
        )

        async def reset(self, set_cb, *, target_config_path=None):
            return await set_cb(str(target_config_path))

        async def auto_revert(self, set_cb, *, target_config_path=None):
            return await set_cb(str(target_config_path))

    assert await correction_handlers._run_locked_room_reset(
        Session(),
        Cam(),
        automatic=automatic,
    )
    assert loaded == [str(restore)]
    assert restore.read_text(encoding="utf-8") == running_graph
    assert predecessor.read_text(encoding="utf-8") == refused_candidate


async def test_room_reversal_does_not_restore_over_new_graph_loaded_at_same_path(
    tmp_path,
):
    """Fresh active_raw distinguishes a real same-name load from an overwrite."""
    from jasper.correction.session import SessionState

    predecessor = tmp_path / "active_speaker_manual_current.yml"
    restore = tmp_path / "sound_snapshot_roomrun_123.yml"
    measurement = tmp_path / "correction_measurement_roomrun_123.yml"
    restore.write_text("filters:\n  old_crossover: {}\n", encoding="utf-8")
    predecessor.write_text("filters:\n  new_crossover: {}\n", encoding="utf-8")
    sess = SimpleNamespace(
        session_id="samepath",
        state=SessionState.FAILED,
        pre_measurement_config_path=predecessor,
        pre_measurement_restore_path=restore,
        measurement_config_path=measurement,
    )

    class Cam:
        async def get_config_file_path(self, *, best_effort=False):
            return str(predecessor)

        async def get_active_config_raw(self, *, best_effort=False):
            return predecessor.read_text(encoding="utf-8")

    assert await correction_handlers._pre_measurement_restore_target(
        sess,
        Cam(),
    ) is None


async def test_running_graph_snapshot_does_not_mint_authority_for_custom_config(
    tmp_path,
):
    """A managed snapshot name must not turn an unknown graph into a carrier."""
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.graph_carrier import CarrierCannotHostEq
    from jasper.sound.profile import SoundProfile

    custom = tmp_path / "advanced-handwritten.yml"
    raw = emit_sound_config(SoundProfile(enabled=False))
    custom.write_text(raw, encoding="utf-8")
    sess = SimpleNamespace(
        session_id="custom",
        cfg=SimpleNamespace(config_dir=tmp_path),
    )

    class Cam:
        async def get_config_file_path(self, *, best_effort=False):
            return str(custom)

        async def get_active_config_raw(self, *, best_effort=False):
            return raw

    with pytest.raises(CarrierCannotHostEq) as exc_info:
        await correction_handlers._snapshot_running_room_graph(sess, Cam())

    assert exc_info.value.reason_code == "unknown_config"
    assert list(tmp_path.glob("sound_snapshot_custom_*.yml")) == []


async def test_reset_safety_failure_preserves_current_and_uses_preemit_snapshot(
    monkeypatch,
    tmp_path,
):
    """Rejected Reset output cannot overwrite or become its own fallback."""
    from jasper.output_topology import save_output_topology
    from jasper.correction.session import SessionState
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    # This test exercises the later candidate-safety rejection.  A reset now
    # leaves an empty topology unconfigured and parked, so declare the valid
    # passive layout that permits the flat candidate to reach that seam.
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_full_range_stereo(), path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    current = tmp_path / "sound_current.yml"
    original = emit_sound_config(SoundProfile(enabled=False))
    current.write_text(original, encoding="utf-8")
    monkeypatch.setenv(
        "JASPER_SOUND_PROFILE_PATH",
        str(tmp_path / "no-saved-profile.json"),
    )
    safety_calls = []

    def fail_candidate_after_snapshot(text, **_kwargs):
        safety_calls.append(text)
        if len(safety_calls) == 2:
            raise RuntimeError("post-write safety refusal")

    monkeypatch.setattr(
        "jasper.correction.runtime_safety.assert_correction_graph_safe",
        fail_candidate_after_snapshot,
    )

    class Cam:
        async def get_config_file_path(self, *, best_effort=False):
            return str(current)

        async def get_active_config_raw(self, *, best_effort=False):
            return original

    sess = SimpleNamespace(
        session_id="resetguard",
        state=SessionState.APPLIED,
        cfg=SimpleNamespace(
            config_dir=tmp_path,
            base_config_path=tmp_path / "base.yml",
        ),
    )

    target = await correction_handlers._resolve_reset_target_async(sess, Cam())

    assert target.name.startswith("sound_snapshot_resetguard_")
    assert correction_handlers._running_graph_body(
        target.read_text(encoding="utf-8")
    ) == correction_handlers._running_graph_body(original)
    assert current.read_text(encoding="utf-8") == original
    rejected = list(tmp_path.glob("sound_reset_resetguard_*.yml"))
    assert len(rejected) == 1
    assert target != rejected[0]
    assert len(safety_calls) == 2


@pytest.mark.parametrize("automatic", [False, True], ids=["reset", "auto-revert"])
@pytest.mark.parametrize(
    "fallback_shape",
    ["corrected-active", "custom", "unreadable", "measurement-baseline"],
)
async def test_room_reversal_rejects_unverified_no_room_fallback(
    monkeypatch,
    tmp_path,
    automatic,
    fallback_shape,
):
    """Reset succeeds only for a readable, managed, allowlisted no-Room graph."""
    from jasper.correction.session import SessionState

    fallback = tmp_path / "sound_current.yml"
    if fallback_shape == "corrected-active":
        fallback.write_text(
            "# Source: "
            "jasper.active_speaker.camilla_yaml."
            "emit_active_speaker_baseline_config\n"
            "filters:\n"
            "  room_peq_0:\n"
            "    type: Biquad\n"
        )
    elif fallback_shape == "custom":
        fallback = tmp_path / "advanced.yml"
        fallback.write_text("pipeline: []\n")
    elif fallback_shape == "measurement-baseline":
        fallback = tmp_path / "correction_measurement_smoke_123.yml"
        fallback.write_text("pipeline: []\n")
    else:
        assert fallback_shape == "unreadable"
    operations = []

    async def reemit_fails(_sess, _cam, **_kwargs):
        raise RuntimeError("carrier re-emit failed")

    monkeypatch.setattr(
        correction_handlers,
        "_write_no_room_correction_config",
        reemit_fails,
    )
    async def snapshot_current(_sess, _cam):
        return fallback, fallback, {
            "authority_valid": True,
            "runtime_block_required": False,
        }

    monkeypatch.setattr(
        correction_handlers,
        "_snapshot_running_room_graph",
        snapshot_current,
    )

    class Cam:
        async def get_config_file_path(self, *, best_effort=False):
            return str(fallback)

    class Session:
        session_id = "corrected-fallback"
        state = SessionState.APPLIED
        cfg = SimpleNamespace(
            config_dir=tmp_path,
            base_config_path=tmp_path / "base.yml",
        )

        async def reset(self, *_args, **_kwargs):
            operations.append("reset")

        async def auto_revert(self, *_args, **_kwargs):
            operations.append("auto-revert")

    with pytest.raises(RuntimeError, match="no verified no-Room graph"):
        await correction_handlers._run_locked_room_reset(
            Session(),
            Cam(),
            automatic=automatic,
        )

    assert operations == []


async def test_reset_accepts_verified_no_room_active_fallback(
    monkeypatch,
    tmp_path,
):
    """A carrier failure may still use a managed, filter-free active graph."""
    from jasper.correction.session import SessionState

    no_room = tmp_path / "sound_current.yml"
    no_room.write_text(
        "# Source: "
        "jasper.active_speaker.camilla_yaml.emit_active_speaker_baseline_config\n"
        "filters: {}\n"
    )

    async def reemit_fails(_sess, _cam, **_kwargs):
        raise RuntimeError("carrier re-emit failed")

    monkeypatch.setattr(
        correction_handlers,
        "_write_no_room_correction_config",
        reemit_fails,
    )
    async def snapshot_current(_sess, _cam):
        return no_room, no_room, {
            "authority_valid": True,
            "runtime_block_required": False,
        }

    monkeypatch.setattr(
        correction_handlers,
        "_snapshot_running_room_graph",
        snapshot_current,
    )
    sess = SimpleNamespace(
        session_id="no-room-fallback",
        state=SessionState.APPLIED,
        cfg=SimpleNamespace(
            config_dir=tmp_path,
            base_config_path=tmp_path / "base.yml",
        ),
    )

    class Cam:
        async def get_config_file_path(self, *, best_effort=False):
            return str(no_room)

    assert (
        await correction_handlers._resolve_reset_target_async(sess, Cam())
        == no_room
    )


@pytest.mark.parametrize("automatic", [False, True], ids=["reset", "auto-revert"])
async def test_failed_room_reversal_preserves_newer_active_graph(
    monkeypatch,
    tmp_path,
    automatic,
):
    """Active Apply between Room Start and failed Apply supersedes predecessor."""
    from jasper.correction.session import SessionState

    old_active = tmp_path / "active-before-room.yml"
    old_active_snapshot = tmp_path / "sound_snapshot_oldactive_123.yml"
    measurement = tmp_path / "correction_measurement_smoke_123.yml"
    new_active = tmp_path / "active-after-room-start.yml"
    no_room_new_active = tmp_path / "sound_current.yml"
    reemitted_from = []
    loaded = []

    class Cam:
        current = str(new_active)

        async def get_config_file_path(self, *, best_effort=False):
            return self.current

        async def set_config_file_path(self, path, *, best_effort=False):
            loaded.append(path)
            self.current = path
            return True

    cam = Cam()

    async def snapshot_current(_sess, current_cam):
        reemitted_from.append(
            await current_cam.get_config_file_path(best_effort=False)
        )
        return new_active, old_active_snapshot, {
            "authority_valid": True,
            "runtime_block_required": False,
        }

    async def reemit_current(
        _sess,
        _current_cam,
        *,
        current_snapshot_path=None,
        bass_profile_summary=None,
    ):
        assert current_snapshot_path == old_active_snapshot
        assert bass_profile_summary is not None
        return no_room_new_active

    monkeypatch.setattr(
        correction_handlers,
        "_snapshot_running_room_graph",
        snapshot_current,
    )
    monkeypatch.setattr(
        correction_handlers,
        "_write_no_room_correction_config",
        reemit_current,
    )

    class Session:
        session_id = "active-superseded-room"
        state = SessionState.FAILED
        pre_measurement_config_path = old_active
        pre_measurement_restore_path = tmp_path / "sound_snapshot_before_1.yml"
        measurement_config_path = measurement
        cfg = SimpleNamespace(
            config_dir=tmp_path,
            base_config_path=tmp_path / "base.yml",
        )

        async def reset(self, set_cb, *, target_config_path=None):
            return await set_cb(str(target_config_path))

        async def auto_revert(self, set_cb, *, target_config_path=None):
            return await set_cb(str(target_config_path))

    assert await correction_handlers._run_locked_room_reset(
        Session(),
        cam,
        automatic=automatic,
    )
    assert reemitted_from == [str(new_active)]
    assert loaded == [str(no_room_new_active)]
    assert str(old_active) not in loaded


def test_apply_restores_listening_volume_when_apply_raises(monkeypatch):
    restored: list[float] = []
    _install_recording_volume_owner(restored)
    sess = _locked_autolevel_session("apply", original=-20.0)
    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(
        correction_capture, "_camilla", lambda: _volume_recording_cam(restored)
    )
    async def authority_current(_cam, _expected):
        return None

    monkeypatch.setattr(
        correction_capture,
        "_assert_room_authority_current",
        authority_current,
    )

    with pytest.raises(RuntimeError):
        correction_handlers._handle_apply(None)

    # The apply exception propagated, but the finally still restored volume.
    assert restored == [-20.0]


def test_room_authority_guard_returns_exact_canonical_bass_summary(
    monkeypatch,
) -> None:
    from jasper.active_speaker import setup_status

    expected = (True, "manual_applied_profile", "layer-a-current")
    summary = MappingProxyType({
        "authority_valid": True,
        "runtime_block_required": False,
    })
    graph = GraphSafety(
        classification=GRAPH_APPROVED_ACTIVE_RUNTIME,
        allowed=True,
        details={"bass_extension_profile_summary": summary},
    )
    classifications = 0

    async def classify(_cam):
        nonlocal classifications
        classifications += 1
        return graph

    class Cam:
        async def get_active_config_raw(self, *, best_effort=False):
            return "running graph"

    monkeypatch.setattr(
        correction_capture,
        "_classify_live_bass_extension_graph",
        classify,
    )
    monkeypatch.setattr(
        setup_status,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {
            "active": True,
            "room_correction_allowed": True,
            "acoustic_commissioning": {
                "decision_schema_version": 1,
                "authority": "manual_applied_profile",
                "layer_a_identity": "layer-a-current",
                "allowed": True,
                "status": "ready",
                "setup_href": "/sound/speaker/crossover/",
            },
        },
    )

    returned = asyncio.run(
        correction_capture._assert_room_authority_current(Cam(), expected)
    )

    assert returned is summary
    assert classifications == 1


def test_unreadable_receipt_mid_run_preserves_a_completed_measurement(
    monkeypatch,
) -> None:
    """A machine fault at the writer boundary must not discard the run.

    The run started under a proven binding and measured six positions. If the
    receipt becomes UNREADABLE between /start and accept, the denied readiness
    collapses the binding to (None, None, None) -- which, treated as an
    authority CHANGE, raises and throws the whole measurement away. An
    unreadable-but-unchanged authority is a device fault, not drift: the guard
    proceeds and returns the graph's bass summary (blocker 2 / ADR-0196).
    """
    from jasper.active_speaker._common import ROOM_AUTHORITY_RECEIPT_UNREADABLE

    expected = (True, "automatic_commissioning_receipt", "layer-a-at-start")
    summary = MappingProxyType({
        "authority_valid": True,
        "runtime_block_required": False,
    })

    async def unreadable_with_graph(_cam):
        raw = {
            "active": True,
            "room_correction_allowed": False,
            "acoustic_commissioning": {
                "decision_schema_version": 1,
                "authority": None,
                "layer_a_identity": None,
                "allowed": False,
                "status": "incomplete",
                "reason": ROOM_AUTHORITY_RECEIPT_UNREADABLE,
                "detail": "the record could not be opened",
                "setup_href": "/sound/speaker/crossover/",
            },
        }
        return raw, GraphSafety(
            classification=GRAPH_APPROVED_ACTIVE_RUNTIME,
            allowed=True,
            details={"bass_extension_profile_summary": summary},
        )

    monkeypatch.setattr(
        correction_capture,
        "_read_room_correction_readiness_with_graph",
        unreadable_with_graph,
    )

    returned = asyncio.run(
        correction_capture._assert_room_authority_current(object(), expected)
    )

    assert returned is summary


@pytest.mark.parametrize(
    ("expected", "discloses"),
    [
        # Started PROVEN, now UNREADABLE: the binding does not match, so if the
        # authority genuinely changed we bank under the prior one -- a fail-OPEN
        # that must be surfaced.
        ((True, "automatic_commissioning_receipt", "layer-a-at-start"), True),
        # Started unproven, still unproven: the binding matches, nothing is
        # ambiguous, so no disclosure.
        ((None, None, None), False),
    ],
)
def test_unreadable_fail_open_at_writer_boundary_is_disclosed(
    monkeypatch, caplog, expected, discloses,
) -> None:
    """Fix 4: proceeding on UNREADABLE keeps the run (blocker 2), but when the
    binding did not match it is a fail-open -- surfaced once, never silent."""
    from jasper.active_speaker._common import ROOM_AUTHORITY_RECEIPT_UNREADABLE
    from jasper.transition_log import TransitionLog

    # Isolate the process-global disclosure gate from other tests.
    monkeypatch.setattr(
        correction_capture,
        "_AUTHORITY_UNCONFIRMED_DISCLOSURE",
        TransitionLog(reminder_sec=3600.0),
    )
    summary = MappingProxyType({
        "authority_valid": True,
        "runtime_block_required": False,
    })

    async def unreadable_with_graph(_cam):
        raw = {
            "active": True,
            "room_correction_allowed": False,
            "acoustic_commissioning": {
                "decision_schema_version": 1,
                "authority": None,
                "layer_a_identity": None,
                "allowed": False,
                "status": "incomplete",
                "reason": ROOM_AUTHORITY_RECEIPT_UNREADABLE,
                "detail": "the record could not be opened",
                "setup_href": "/sound/speaker/crossover/",
            },
        }
        return raw, GraphSafety(
            classification=GRAPH_APPROVED_ACTIVE_RUNTIME,
            allowed=True,
            details={"bass_extension_profile_summary": summary},
        )

    monkeypatch.setattr(
        correction_capture,
        "_read_room_correction_readiness_with_graph",
        unreadable_with_graph,
    )

    with caplog.at_level(logging.WARNING):
        returned = asyncio.run(
            correction_capture._assert_room_authority_current(object(), expected)
        )

    assert returned is summary  # always proceeds -- the run is never discarded
    unconfirmed = [
        record for record in caplog.records
        if "layer_a_authority_unconfirmed" in record.getMessage()
    ]
    assert len(unconfirmed) == (1 if discloses else 0)


def test_apply_rejects_layer_a_change_inside_writer_boundary(monkeypatch):
    from jasper.correction.session import SessionState

    applied = []

    class Session:
        session_id = "authority-race"
        state = SessionState.READY
        confidence_report = None
        config_path = None
        room_authority_binding = (
            True,
            "manual_applied_profile",
            "layer-a-at-start",
        )

        async def apply(self, *_args, **kwargs):
            await kwargs["prepare_guard"]()
            applied.append(True)

    async def changed_authority(_cam):
        return {
            "active": True,
            "room_correction_allowed": True,
            "acoustic_commissioning": {
                "decision_schema_version": 1,
                "authority": "manual_applied_profile",
                "layer_a_identity": "layer-a-before-apply",
                "allowed": True,
                "status": "ready",
                "setup_href": "/sound/speaker/crossover/",
            },
        }

    monkeypatch.setattr(correction_capture, "_get_or_create_session", Session)
    monkeypatch.setattr(correction_capture, "_camilla", lambda: object())
    async def changed_authority_with_graph(cam):
        return await changed_authority(cam), GraphSafety(
            classification=GRAPH_APPROVED_ACTIVE_RUNTIME,
            allowed=True,
            details={
                "bass_extension_profile_summary": {
                    "authority_valid": True,
                    "runtime_block_required": False,
                }
            },
        )

    monkeypatch.setattr(
        correction_capture,
        "_read_room_correction_readiness_with_graph",
        changed_authority_with_graph,
    )
    monkeypatch.setattr(
        correction_handlers,
        "_maybe_restore_main_volume",
        lambda _sess, _cam: None,
    )

    with pytest.raises(RuntimeError, match="authority changed"):
        correction_handlers._handle_apply(None)

    assert applied == []


def test_reset_restores_listening_volume_when_reset_raises(monkeypatch, tmp_path):
    restored: list[float] = []
    _install_recording_volume_owner(restored)
    sess = _locked_autolevel_session(
        "reset",
        original=-18.0,
        config_dir=tmp_path,
    )
    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(
        correction_capture, "_camilla", lambda: _volume_recording_cam(restored)
    )

    with pytest.raises(RuntimeError):
        correction_handlers._handle_reset(None)

    assert restored == [-18.0]


def test_reset_quiesces_audio_under_intent_before_resolving_graph(
    monkeypatch,
    tmp_path,
):
    """No ramp/sweep write may land after reset resolves or reloads its graph."""
    from jasper.correction.session import AutolevelData, AutolevelStatus, SessionState

    order: list[str] = []

    class _FakeSession:
        session_id = "ramping-reset"
        state = SessionState.NEEDS_NOISE_CAPTURE
        # Terminal status with active cleanup reproduces the original race:
        # Reset must key off run ownership, not the public status enum.
        autolevel = AutolevelData(status=AutolevelStatus.LOCKED)
        autolevel_run_in_progress = True
        reset_intent = object()
        cfg = SimpleNamespace(config_dir=tmp_path)

        async def begin_autolevel_reset(self):
            order.append("intent-and-ramp-quiesced")
            self.autolevel.status = AutolevelStatus.CANCELLED
            return self.reset_intent

        async def stop_background_audio_for_reset(self):
            order.append("sweep-cancelled-and-reaped")
            return True

        async def end_autolevel_reset(self, intent):
            assert intent is self.reset_intent
            order.append("intent-released")
            return True

        async def reset(self, set_cb, **kwargs):
            order.append("reset")

    sess = _FakeSession()
    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(
        correction_capture, "_camilla", lambda: _volume_recording_cam([])
    )
    async def resolve(*_args):
        order.append("resolve")
        return Path("/tmp/reset.yml")

    monkeypatch.setattr(correction_handlers, "_resolve_reset_target_async", resolve)

    correction_handlers._handle_reset(None)

    assert order == [
        "intent-and-ramp-quiesced",
        "sweep-cancelled-and-reaped",
        "resolve",
        "reset",
        "intent-released",
    ]


@pytest.mark.parametrize("automatic", [False, True], ids=["reset", "auto-revert"])
async def test_room_reversal_resolves_and_loads_after_concurrent_active_writer(
    monkeypatch,
    tmp_path,
    automatic,
):
    """A legal Active writer must finish before Room chooses its reset graph."""
    from jasper.dsp_apply import dsp_writer_lock

    current = {"path": "active-old.yml"}
    resolved_from = []
    loaded = []

    async def resolve(_sess, _cam):
        resolved_from.append(current["path"])
        return tmp_path / f"no-room-from-{current['path']}"

    monkeypatch.setattr(correction_handlers, "_resolve_reset_target_async", resolve)

    class Session:
        cfg = SimpleNamespace(config_dir=tmp_path)

        async def reset(self, set_cb, *, target_config_path=None):
            return await set_cb(str(target_config_path))

        async def auto_revert(self, set_cb, *, target_config_path=None):
            return await set_cb(str(target_config_path))

    class Cam:
        async def set_config_file_path(self, path, *, best_effort=False):
            loaded.append(path)
            current["path"] = path
            return True

    async with dsp_writer_lock(tmp_path, source="active_apply"):
        reversal = asyncio.create_task(
            correction_handlers._run_locked_room_reset(
                Session(),
                Cam(),
                automatic=automatic,
            )
        )
        await asyncio.sleep(0.1)
        assert not reversal.done()
        assert resolved_from == []
        assert loaded == []
        current["path"] = "active-new.yml"

    assert await reversal is True
    assert resolved_from == ["active-new.yml"]
    assert loaded == [str(tmp_path / "no-room-from-active-new.yml")]


@pytest.mark.parametrize("automatic", [False, True], ids=["reset", "auto-revert"])
async def test_concurrent_active_writer_cannot_publish_during_room_reversal(
    monkeypatch,
    tmp_path,
    automatic,
):
    """Room must retain the writer lock from target derivation through load."""
    from jasper.dsp_apply import dsp_writer_lock

    resolution_started = asyncio.Event()
    allow_resolution = asyncio.Event()
    load_started = asyncio.Event()
    allow_load = asyncio.Event()
    order = []

    async def resolve(_sess, _cam):
        resolution_started.set()
        await allow_resolution.wait()
        return tmp_path / "room-no-room.yml"

    monkeypatch.setattr(correction_handlers, "_resolve_reset_target_async", resolve)

    class Session:
        cfg = SimpleNamespace(config_dir=tmp_path)

        async def _reverse(self, set_cb, target_config_path):
            load_started.set()
            await allow_load.wait()
            return await set_cb(str(target_config_path))

        async def reset(self, set_cb, *, target_config_path=None):
            return await self._reverse(set_cb, target_config_path)

        async def auto_revert(self, set_cb, *, target_config_path=None):
            return await self._reverse(set_cb, target_config_path)

    class Cam:
        async def set_config_file_path(self, path, *, best_effort=False):
            order.append(("room-load", path))
            return True

    async def active_apply():
        async with dsp_writer_lock(tmp_path, source="active_apply"):
            order.append(("active-publish", "active-new.yml"))

    reversal = asyncio.create_task(
        correction_handlers._run_locked_room_reset(
            Session(),
            Cam(),
            automatic=automatic,
        )
    )
    await asyncio.wait_for(resolution_started.wait(), timeout=1.0)
    active = asyncio.create_task(active_apply())
    allow_resolution.set()
    await asyncio.wait_for(load_started.wait(), timeout=1.0)

    # If Room released the shared lock after deriving the target but before
    # loading it, Active would acquire during this deliberately paused load.
    await asyncio.sleep(0.1)
    assert not active.done()
    assert order == []

    allow_load.set()
    assert await reversal is True
    await active
    assert order == [
        ("room-load", str(tmp_path / "room-no-room.yml")),
        ("active-publish", "active-new.yml"),
    ]


def test_reset_releases_intent_when_audio_quiescence_fails(monkeypatch):
    """A failed Stop never wedges every later reset behind a leaked intent."""
    from jasper.correction.session import SessionState

    order: list[str] = []

    class _FakeSession:
        session_id = "quiescence-failure"
        state = SessionState.SWEEPING
        reset_intent = object()

        async def begin_autolevel_reset(self):
            order.append("intent")
            return self.reset_intent

        async def stop_background_audio_for_reset(self):
            order.append("stop")
            raise RuntimeError("audio cleanup failed")

        async def end_autolevel_reset(self, intent):
            assert intent is self.reset_intent
            order.append("intent-released")
            return True

    sess = _FakeSession()
    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(
        correction_capture, "_camilla", lambda: _volume_recording_cam([])
    )

    with pytest.raises(RuntimeError, match="audio cleanup failed"):
        correction_handlers._handle_reset(None)

    assert order == ["intent", "stop", "intent-released"]


@pytest.mark.parametrize("settled", [True, False])
def test_maybe_restore_respects_measurement_state(tmp_path, settled):
    from jasper.correction.autolevel import AutolevelData, AutolevelStatus
    from jasper.correction.session import SessionState

    written = []
    _install_recording_volume_owner(written)
    sess = make_measurement_session(tmp_path)
    sess.autolevel = AutolevelData(
        status=AutolevelStatus.LOCKED, original_main_volume_db=-20.0,
    )
    sess._main_volume_setter = correction_capture._household_level_door()
    states = (
        (SessionState.APPLIED, SessionState.IDLE, SessionState.FAILED)
        if settled else
        (SessionState.PREPARING, SessionState.SWEEPING,
         SessionState.ANALYZING, SessionState.VERIFYING)
    )
    for state in states:
        sess.state = state
        correction_handlers._maybe_restore_main_volume(sess, None)
    assert written == ([-20.0] if settled else [])
    assert sess.autolevel.restored is settled


def test_e2e_reset_while_busy_returns_409(monkeypatch, tmp_path):
    # A reset rejected because a sweep/analysis is in flight is a state
    # conflict, not a server error — the dispatch maps SessionBusyError to 409
    # (a stale/buggy client hitting /reset mid-sweep; the UI never does).
    from jasper.correction.session import SessionBusyError, SessionState

    class FakeSession:
        session_id = "busy-reset"
        state = SessionState.SWEEPING
        cfg = SimpleNamespace(config_dir=tmp_path)

        async def stop_background_audio_for_reset(self):
            raise SessionBusyError(
                "cannot reset while sweeping — analysis is in progress"
            )

        async def reset(self, set_cb):
            pytest.fail("busy reset must refuse before graph resolution")

    monkeypatch.setattr(
        correction_capture, "_get_or_create_session", lambda: FakeSession(),
    )

    server, base = _start_server()
    try:
        e = request_with_csrf(
            base, "/reset", b"{}",
            content_type="application/json", expect_status=409,
        )
        body = json.loads(e.read().decode())
        assert "in progress" in body["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_reset_safety_refusal_returns_422(monkeypatch):
    from jasper.correction.runtime_safety import CorrectionRuntimeSafetyError

    def fake_reset(handler):
        raise CorrectionRuntimeSafetyError("no legal graph is available")

    monkeypatch.setattr(correction_handlers, "_handle_reset", fake_reset)
    server, base = _start_server()
    try:
        e = request_with_csrf(
            base,
            "/reset",
            b"{}",
            content_type="application/json",
            expect_status=422,
        )
        body = json.loads(e.read().decode())
        assert "no legal graph" in body["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_the_level_match_restores_share_one_owner_door():
    """W12's level-match half, routed as itself.

    Six restore sites answered one question — give the household its level
    back — and each bound its own raw ``cam.set_volume_db(..., best_effort=
    False)``. They now share ``_household_level_door()``, so the flag is the
    owner's contract once rather than six per-site decisions that can drift.
    """
    from jasper.volume_owner import VolumeOwner, install_volume_owner

    written: list[float] = []

    async def _set(db: float) -> bool:
        written.append(float(db))
        return True

    async def _get() -> float:
        return written[-1] if written else 0.0

    install_volume_owner(
        VolumeOwner(set_fader_db=_set, get_fader_db=_get)
    )

    door = correction_capture._household_level_door()
    assert asyncio.run(door(-19.5)) is True
    assert written == [-19.5]


def test_a_level_match_restore_without_an_owner_reports_not_in_effect(caplog):
    """An absent owner is a registration defect, so it is LOUD and it is not
    silently swallowed — but it must not mint a second owner either. The door
    reports "not in effect", which every caller already treats as a failed
    restore."""
    from jasper.volume_owner import install_volume_owner

    install_volume_owner(None)
    door = correction_capture._household_level_door()

    with caplog.at_level(logging.CRITICAL):
        assert asyncio.run(door(-19.5)) is False

    assert any(
        "level_match_restore_owner_absent" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("ending", [
    "playback_failure", "capture_timeout", "restore_false", "restore_exception",
    "restore_readback", "maxed_out", "cancel", "apply", "reset", "verified",
])
def test_room_terminal_cleanup_releases_real_owner_and_retries(
    monkeypatch, tmp_path, ending,
):
    from jasper import measurement_window as window_module
    from jasper.correction import playback
    from jasper.correction.session import AutolevelStatus, SessionState
    from jasper.volume_owner import ClaimKind, VolumeOwner, install_volume_owner

    class Fader:
        db = -18.0
        failure = None

        async def set(self, db):
            if self.failure == "restore_exception":
                raise OSError("injected fader failure")
            if self.failure == "restore_false":
                return False
            if self.failure != "restore_readback":
                self.db = db
            return True

        async def get_volume_db(self, **_kwargs):
            return self.db

    fader = Fader()
    owner = VolumeOwner(set_fader_db=fader.set, get_fader_db=fader.get_volume_db)
    install_volume_owner(owner)
    run = correction_capture._run_async
    run(owner.declare_household_level_db(-18.0))
    sess = make_measurement_session(tmp_path)
    sess.state = SessionState.NEEDS_NOISE_CAPTURE
    sess._local_capture_setup_bound = True
    stopped = threading.Event()

    class Player:
        def __init__(self, _path):
            self.stop = asyncio.Event()

        async def play(self):
            if ending == "cancel":
                await sess.cancel_autolevel()
            elif ending != "maxed_out":
                await sess.lock_autolevel()
            await self.stop.wait()

        def cancel(self):
            self.stop.set()
            stopped.set()

    monkeypatch.setattr(correction_capture, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(correction_capture, "_camilla", lambda: fader)
    monkeypatch.setattr(window_module, "measurement_window", lambda: nullcontext())
    monkeypatch.setattr(playback, "_ensure_tone_wav", lambda **_kwargs: tmp_path / "tone.wav")
    monkeypatch.setattr(playback, "TonePlayer", Player)
    if ending == "maxed_out":
        run_autolevel = sess.run_autolevel

        async def fast_run(**kwargs):
            await run_autolevel(**kwargs, start_db=-12.0, end_db=-12.0, fade_step_s=0)

        monkeypatch.setattr(sess, "run_autolevel", fast_run)
    assert correction_handlers._handle_autolevel_start(None)["started"]
    wait_until_sync(lambda: not sess.autolevel_run_in_progress)
    assert stopped.is_set()

    if ending not in {"maxed_out", "cancel"}:
        assert owner.holds_kind(ClaimKind.SESSION_MEASUREMENT)
        assert sess.autolevel.status is AutolevelStatus.LOCKED
    if ending == "playback_failure":
        async def broken_play(*_args, **_kwargs):
            raise OSError("injected playback failure")

        with pytest.raises(OSError):
            run(sess.prepare_and_play_sweep(broken_play))
    elif ending == "capture_timeout":
        sess.capture_timeout_sec = 0.01
        run(sess._set_state(SessionState.AWAITING_CAPTURE))
        wait_until_sync(
            lambda: sess.state is SessionState.FAILED
            and sess._autolevel_reset_intent is None,
        )
    elif ending.startswith("restore_"):
        fader.failure = ending
        run(sess._fail("injected measurement failure"))
        assert not owner.holds_kind(ClaimKind.SESSION_MEASUREMENT)
        assert sess.autolevel.restored is False
        assert sess.autolevel.snapshot()["restore_error"] is not None
        fader.failure = None
        correction_handlers._maybe_restore_main_volume(sess, fader)
    elif ending in {"apply", "reset", "verified"}:
        sess.state = {
            "apply": SessionState.APPLIED, "reset": SessionState.IDLE,
            "verified": SessionState.VERIFIED,
        }[ending]
        correction_handlers._maybe_restore_main_volume(sess, fader)

    assert fader.db == -18.0
    assert sess.autolevel.restored is True
    assert sess.autolevel.snapshot()["restore_error"] is None
    assert not owner.holds_kind(ClaimKind.SESSION_MEASUREMENT)
    assert run(owner.declare_household_level_db(-30.0))
    assert fader.db == -30.0
    correction_handlers._maybe_restore_main_volume(sess, fader)
    assert fader.db == -30.0
    claim = run(owner.acquire_level(ClaimKind.SESSION_MEASUREMENT, -35.0))
    assert fader.db == -35.0
    run(owner.release(claim))
    sess._cancel_capture_timeout()


def test_the_level_match_claim_is_taken_once_moved_and_released():
    """W12's level-match half as ONE cross-request claim.

    Three writers share it — the ramp's own setter, the before-sweep
    re-assertion, and the restore that ends it. Taken once, MOVED after, and
    released by the restore in a single write.

    A household level is declared first on purpose: without one a release
    settles to nothing and writes nothing, so a release-per-step regression
    would be invisible and the mutant would pass clean. With one, any
    reacquire strobes through -15.0 and the sequence assertion catches it.
    """
    from jasper.volume_owner import VolumeOwner, install_volume_owner

    written: list[float] = []
    live = {"db": 0.0}

    async def _set(db: float) -> bool:
        live["db"] = float(db)
        written.append(float(db))
        return True

    async def _get() -> float:
        return live["db"]

    owner = VolumeOwner(set_fader_db=_set, get_fader_db=_get)
    install_volume_owner(owner)
    asyncio.run(owner.declare_household_level_db(-15.0))
    written.clear()
    correction_capture._LEVEL_MATCH_CLAIM = None

    async def _drive() -> None:
        for db in (-40.0, -39.0, -38.0):
            assert await correction_capture._assert_level_match_level(db) is True
        assert await correction_capture._assert_level_match_level(-38.0) is True
        assert await correction_capture._household_level_door()(-15.0) is True

    asyncio.run(_drive())

    # The re-assertion at the level already held writes NOTHING: every
    # settle reads first, so arbitration is not churn. That is the owner's
    # property, and routing inherits it instead of re-sending the level.
    assert written == [-40.0, -39.0, -38.0, -15.0]
    assert correction_capture._LEVEL_MATCH_CLAIM is None
    assert owner.declared_level_db() == -15.0


def test_a_refused_level_match_level_is_answered_not_raised():
    """`ensure_level_match_volume` and the ramp both branch on the answer, so a
    claim that cannot be established comes back False. A conflict with a
    measurement claim another journey still holds takes the same path."""
    from jasper.volume_owner import (
        ClaimKind,
        VolumeOwner,
        install_volume_owner,
    )

    live = {"db": 0.0}

    async def _set(db: float) -> bool:
        live["db"] = float(db)
        return True

    async def _get() -> float:
        return live["db"]

    owner = VolumeOwner(set_fader_db=_set, get_fader_db=_get)
    install_volume_owner(owner)
    correction_capture._LEVEL_MATCH_CLAIM = None
    asyncio.run(owner.acquire_level(ClaimKind.SESSION_MEASUREMENT, -30.0))

    assert asyncio.run(correction_capture._assert_level_match_level(-20.0)) is False
    assert correction_capture._LEVEL_MATCH_CLAIM is None
