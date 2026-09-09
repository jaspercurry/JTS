# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the measurement daemon's shared seams and HTTP dispatch.

  1. The capture slot, its idle hold and the ``_run_async`` bridge every
     measurement route shares.
  2. Microphone calibration: fetch, upload, the household-mic record and
     the setup reference the crossover walk resolves through it.
  3. Healthz returns plain-text "ok" so systemd / curl probes work.
  4. End-to-end via a real ThreadingHTTPServer to confirm the routes
     dispatch from real HTTP — same shape as test_voice_setup.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import io
import inspect
import json
import logging
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
import threading
import urllib.error
import urllib.request
from email.message import Message
from http.server import ThreadingHTTPServer

import pytest


from jasper.web import (
    correction_capture,
    correction_handlers,
    correction_runtime,
    correction_setup,
)
from jasper.platform.systemd import no_hold

from ._async_wait import DEFAULT_SIGNAL_TIMEOUT_S, wait_until_sync
from ._web_test_helpers import make_csrf_session, request_with_csrf


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

    monkeypatch.setattr(correction_runtime, "read_json_body", lambda _h: {})
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

    monkeypatch.setattr(correction_runtime, "read_json_body", lambda _h: {})
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
            correction_runtime.run_async(operation(), timeout=0.05)
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


def test_ensure_loop_hands_concurrent_callers_one_running_loop(monkeypatch):
    """Callers arriving DURING loop startup all get the one loop, running.

    Two loops means two capture owners. Gating re-creation on
    ``_loop.is_running()`` read False between ``Thread.start()`` and
    ``run_forever()``, so a caller landing in that window built a second loop
    despite the lock. The gate below holds the window open for the whole race
    rather than hoping to hit it.
    """
    prior_loop = correction_runtime._loop
    prior_thread = correction_runtime._loop_thread
    prior_running = correction_runtime._loop_running.is_set()
    before = {
        t for t in threading.enumerate() if t.name == "jasper-correction-loop"
    }
    correction_runtime._loop = None
    correction_runtime._loop_thread = None
    correction_runtime._loop_running.clear()

    open_the_gate = threading.Event()
    at_the_gate = threading.Semaphore(0)
    built: list = []
    real_run_loop = correction_runtime._run_loop

    def gated_run_loop(loop, running):
        built.append(loop)
        at_the_gate.release()
        open_the_gate.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        real_run_loop(loop, running)

    monkeypatch.setattr(correction_runtime, "_run_loop", gated_run_loop)

    workers = 8
    seen: list = []
    seen_lock = threading.Lock()

    def call_ensure_loop():
        loop = correction_runtime.ensure_loop()
        with seen_lock:
            seen.append(loop)

    threads = [
        threading.Thread(target=call_ensure_loop, daemon=True)
        for _ in range(workers)
    ]
    try:
        threads[0].start()
        # The first loop thread is now parked before run_forever(): every
        # later caller arrives inside the startup window.
        assert at_the_gate.acquire(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        for thread in threads[1:]:
            thread.start()
        assert not at_the_gate.acquire(timeout=0.2), (
            "a caller inside the startup window started a second loop"
        )
        open_the_gate.set()
        for thread in threads:
            thread.join(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
            assert not thread.is_alive()
        assert len(built) == 1
        assert len(seen) == workers
        assert all(loop is seen[0] for loop in seen)
        assert seen[0].is_running()
        started = {
            t for t in threading.enumerate() if t.name == "jasper-correction-loop"
        } - before
        assert len(started) == 1
    finally:
        open_the_gate.set()
        correction_runtime._loop = prior_loop
        correction_runtime._loop_thread = prior_thread
        if prior_running:
            correction_runtime._loop_running.set()
        else:
            correction_runtime._loop_running.clear()
        for loop in built:
            loop.call_soon_threadsafe(loop.stop)
        for thread in threading.enumerate():
            if thread.name == "jasper-correction-loop" and thread not in before:
                thread.join(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        for loop in built:
            if not loop.is_running():
                loop.close()


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
        correction_runtime,
        "RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S",
        0.01,
    )
    monkeypatch.setattr(
        correction_runtime,
        "log_event",
        lambda _logger, event, **_fields: (
            drain_alarm.set()
            if event == "correction.async_cancel_drain_timeout"
            else None
        ),
    )

    def invoke():
        try:
            correction_runtime.run_async(operation(), timeout=0.01)
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


def test_read_json_body_rejects_invalid_content_length():
    class Handler:
        headers = {"Content-Length": "not-a-number"}
        rfile = io.BytesIO()

    with pytest.raises(correction_runtime.BadRequest, match="Content-Length"):
        correction_runtime.read_json_body(Handler())


def test_read_wav_body_rejects_invalid_content_length():
    class Handler:
        headers = {"Content-Length": "not-a-number"}
        rfile = io.BytesIO()

    with pytest.raises(correction_runtime.BadRequest, match="Content-Length"):
        correction_runtime.read_wav_body(Handler())


def test_read_wav_body_rejects_large_or_incomplete_body():
    class TooLarge:
        headers = {"Content-Length": "5"}
        rfile = io.BytesIO(b"12345")

    with pytest.raises(correction_runtime.BadRequest, match="too large"):
        correction_runtime.read_wav_body(TooLarge(), max_bytes=4)

    class Incomplete:
        headers = {"Content-Length": "5"}
        rfile = io.BytesIO(b"123")

    with pytest.raises(correction_runtime.BadRequest, match="incomplete"):
        correction_runtime.read_wav_body(Incomplete())


def test_test_tone_wav_is_generated_and_cached(tmp_path):
    """First call generates the WAV; second call reuses the cache
    file (no re-generation). Cache key is the parameter tuple."""
    from jasper.audio_measurement.playback import ensure_sine_wav
    p1 = ensure_sine_wav(
        freq_hz=1000, duration_s=2.0, dbfs=-18.0,
        sample_rate=48000, cache_dir=tmp_path,
    )
    assert p1.exists()
    mtime1 = p1.stat().st_mtime
    # Second call → same path, cache hit.
    p2 = ensure_sine_wav(
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
    from jasper.audio_measurement.playback import ensure_sine_wav

    wav_path = ensure_sine_wav(
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


def _post_with_csrf(base: str, path: str, data: bytes, **kwargs):
    """POST with a CSRF cookie minted from a page this daemon serves.

    The mint page has to be one the daemon renders through ``begin_request``;
    ``/sync`` is the cheapest of them.
    """
    kwargs.setdefault("session", make_csrf_session(base, page_path="/sync"))
    return request_with_csrf(base, path, data, **kwargs)


def _start_server() -> tuple[ThreadingHTTPServer, str]:
    server = correction_setup.make_server(
        ("127.0.0.1", 0), hostname="jts.local",
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


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


def test_e2e_test_tone_plays_through_the_dispatcher(tmp_path, monkeypatch):
    """POST /test-tone answers with what it played, over real HTTP.

    The tone WAV and the ALSA spawn are the only stubs: the route, its CSRF
    guard, the measurement window and the JSON contract are the real ones.
    """
    import contextlib

    from jasper.audio_measurement import correction_lane, playback as am_playback
    import jasper.measurement_window as measurement_window_mod

    monkeypatch.setattr(correction_lane, "CORRECTION_TONE_DIR", tmp_path)
    monkeypatch.setattr(correction_lane, "correction_play_device", lambda: "null")
    played: list[tuple[str, str, float]] = []

    async def _play_wav(wav_path, *, alsa_device, timeout_s):
        played.append((str(wav_path), alsa_device, timeout_s))
        return None

    monkeypatch.setattr(am_playback, "play_wav", _play_wav)

    @contextlib.asynccontextmanager
    async def _window(**_kw):
        yield None

    monkeypatch.setattr(measurement_window_mod, "measurement_window", _window)

    server, base = _start_server()
    try:
        resp = _post_with_csrf(
            base,
            "/test-tone",
            json.dumps({"duration_s": 2.0}).encode("utf-8"),
            content_type="application/json",
        )
        payload = json.loads(resp.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()

    assert payload == {"played": True, "duration_s": 2.0}
    assert len(played) == 1
    wav_path, alsa_device, timeout_s = played[0]
    assert wav_path.startswith(str(tmp_path))
    assert alsa_device == "null"
    assert timeout_s == 7.0


def test_e2e_healthz_returns_plain_ok():
    """systemd's `Type=notify` could replace this later, but for now a
    simple HTTP-200 / "ok" body is what makes a `curl` against the daemon's
    own port a valid liveness probe — and also lets jasper-doctor add a
    measurement-subsystem check without parsing JSON."""
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
        resp = _post_with_csrf(
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
        resp = _post_with_csrf(
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
        e = _post_with_csrf(
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
        e = _post_with_csrf(
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
        e = _post_with_csrf(
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
    from jasper.audio_measurement.household_mic import (
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
    from jasper.audio_measurement.household_mic import read_household_mic

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
    from jasper.audio_measurement.household_mic import read_household_mic

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
    from jasper.audio_measurement import household_mic

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
    from jasper.audio_measurement.household_mic import (
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
    caplog.set_level(logging.WARNING, logger="jasper.audio_measurement.household_mic")
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
        resp = _post_with_csrf(
            base,
            "/calibration/fetch",
            payload,
            content_type="application/json",
        )
        assert resp.status == 200
    finally:
        server.shutdown()
        server.server_close()

    from jasper.audio_measurement.household_mic import read_household_mic

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
        resp = _post_with_csrf(
            base,
            "/calibration/upload",
            payload,
            content_type="application/json",
        )
        assert resp.status == 200
    finally:
        server.shutdown()
        server.server_close()

    from jasper.audio_measurement.household_mic import read_household_mic

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
    from jasper.audio_measurement.household_mic import (
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
    inferred from `resolved_household_mic()` having just succeeded — so a
    resolver hiccup between the two calls degrades to "no one-tap" (the hint
    still ships, just without `resolvable`) instead of dropping the whole
    hint or raising."""
    cal_root = tmp_path / "cal"
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(cal_root))
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    from jasper.audio_measurement.calibration import store_calibration
    from jasper.audio_measurement import household_mic
    from jasper.audio_measurement.household_mic import (
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
        # First call is `resolved_household_mic()` building the hint's other
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


def test_sync_analyze_rejects_oversized_capture_before_body_read():
    handler_cls = correction_setup._make_handler_class(
        hostname="jts.local", idle_hold=nullcontext,
    )
    handler = handler_cls.__new__(handler_cls)
    handler.path = "/sync/analyze"
    handler.headers = Message()
    handler.headers["Content-Length"] = str(2 * 1024 * 1024 + 1)
    handler.rfile = io.BytesIO(b"")
    sent: dict = {}

    def _send_json(payload, status=200):
        sent["payload"] = payload
        sent["status"] = int(status)

    handler._send_json = _send_json

    correction_setup._dispatch_sync(handler)

    assert sent["status"] == 400
    assert "WAV body too large" in sent["payload"]["error"]


# --- Bug 1 regression: calibration↔device mismatch backstop -----------------
# A vendor measurement-mic calibration applied to phone-built-in-mic audio
# silently invalidates the measurement. The browser blocks it, but this
# server-side gate is the reliable backstop. Reproduces the cmm31555 iMM-6C
# run on 2026-06-04 where input_device.browser_label was "iPhone Microphone".


