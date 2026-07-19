# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free tests for the /correction/ wizard (canonical design system).

The room-correction page is HARDWARE/BROWSER-CRITICAL — the real measurement
flow (getUserMedia, the sweep, CamillaDSP apply) only runs on the Pi. These
tests cover the parts that can be checked off-device: the page renders the
canonical document shell, the relocated behaviour ships as an ES module (no
inline IIFE remains), the routes still resolve, and the CSRF guard still
fires. Network / CamillaDSP / session imports are lazy inside the handlers,
so a static render needs no hardware.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from jasper.web import correction_setup


_CORRECTION_MODULE = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "assets" / "correction" / "js" / "main.js"
)


def _module_js() -> str:
    return _CORRECTION_MODULE.read_text()


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
        correction_setup._run_async(never_finishes(), timeout=0.01)
    assert cancelled.wait(timeout=2)


def test_room_graph_mutation_has_no_cancelling_outer_deadline(monkeypatch):
    import asyncio

    seen = {}

    async def operation():
        return "done"

    def run(coro, *, timeout):
        seen["timeout"] = timeout
        return asyncio.run(coro)

    monkeypatch.setattr(correction_setup, "_run_async", run)

    assert correction_setup._run_graph_mutation(operation()) == "done"
    assert seen == {"timeout": None}


def test_all_room_graph_mutation_callers_use_terminal_runner():
    import inspect

    callers = (
        correction_setup._handle_start,
        correction_setup._handle_apply,
        correction_setup._handle_reset,
        correction_setup._maybe_auto_revert,
    )
    for caller in callers:
        source = inspect.getsource(caller)
        assert "_run_graph_mutation(" in source


# ---------------------------------------------------------------------------
# Page render — canonical shell.
# ---------------------------------------------------------------------------


def _render() -> str:
    return correction_setup._render_page(
        "jts.local",
        csrf_token="tok-correction-123456789012345678901234",
    ).decode("utf-8")


def test_shared_measurement_start_blocker_prioritizes_reserved_start(
    monkeypatch,
):
    class ActiveState:
        value = "sweeping"

    class ActiveSession:
        state = ActiveState()

    monkeypatch.setattr(correction_setup, "_session", ActiveSession())
    monkeypatch.setattr(correction_setup, "_start_in_progress", False)
    assert correction_setup._correction_start_blocker() == "sweeping"

    monkeypatch.setattr(correction_setup, "_start_in_progress", True)
    assert correction_setup._correction_start_blocker() == "starting"

    monkeypatch.setattr(correction_setup, "_session", None)
    monkeypatch.setattr(correction_setup, "_start_in_progress", False)
    assert correction_setup._correction_start_blocker() is None


def test_render_uses_canonical_shell():
    html = _render()
    assert html.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in html
    assert "app-header" in html
    assert 'name="jts-csrf"' in html
    assert "tok-correction-123456789012345678901234" in html


def test_render_links_page_css_and_module():
    html = _render()
    assert "/assets/correction/correction.css?v=" in html
    assert "/assets/correction/js/main.js" in html
    assert 'type="module"' in html


def test_render_has_correction_measurement_tabs():
    html = _render()
    assert 'aria-label="Correction measurement type"' in html
    assert 'href="/correction/room/"' in html
    assert 'href="/correction/crossover/"' in html
    assert 'href="/correction/bass/"' in html
    assert 'aria-current="page" href="/correction/room/"' in html


def test_render_has_no_inline_script_iife():
    """The page behaviour was relocated into the ES module; the legacy
    inline <script> IIFE must be gone (gating: no inline JS on a migrated
    page)."""
    html = _render()
    assert "(function () {" not in html
    # Old hand-rolled shell + injection markers must be gone too.
    assert "__STYLE__" not in html
    assert "__DIALOG_HELPERS__" not in html
    assert "__CSRF_FETCH_HELPERS__" not in html


def test_render_carries_required_sample_rate_for_module():
    """The module reads the required capture rate off the page rather than
    hardcoding it; the server stays the source of truth."""
    html = _render()
    assert f'data-required-sr="{correction_setup.REQUIRED_SAMPLE_RATE}"' in html


def test_render_back_link_is_absolute_http():
    """/correction/ is HTTPS but the dashboard at / is plain HTTP, so the
    Home affordance must be an absolute http:// link, not a relative '/'."""
    html = _render()
    assert 'href="http://jts.local/"' in html


def test_render_has_one_root_for_each_envelope_section():
    html = _render()
    section_ids = {
        "current-correction", "run-defaults", "readiness-blocker",
        "capture-handoff", "placement", "capture-setup",
        "local-certificate-warning", "level-check", "position-capture",
        "measurement-review", "apply-status", "verification",
        "result-proof", "tuning", "reports",
    }
    for section_id in section_ids:
        assert html.count(f'data-envelope-section="{section_id}"') == 1

    for deleted_id in (
        "relay-panel", "relay-start-capture", "advanced-correction-options",
        "mic-panel", "measurement-reports", "measure-section",
        "run-measurement", "apply-correction", "verify-correction",
        "repeat-position", "continue-position", "start",
    ):
        assert f'id="{deleted_id}"' not in html


def test_render_keeps_only_plain_local_certificate_warning():
    html = _render()
    assert "browser will warn about the speaker's local certificate" in html
    assert "/jts-root-ca.crt" not in html
    assert "Certificate Trust Settings" not in html
    assert "mkcert" not in html
    assert 'id="readiness-blocker-action" class="btn hidden" href=""' in html
    assert 'id="readiness-blocker-action" class="btn" href="/sound/"' not in html


def test_render_leaves_household_default_copy_to_the_envelope():
    html = _render()

    assert '<p id="run-defaults-summary"></p>' in html
    assert "Measuring 6 positions with the flat target" not in html
    assert html.count('id="change-run-defaults"') == 1
    assert 'aria-controls="measurement-options"' in html
    assert 'aria-expanded="false"' in html
    assert (
        '<option value="6" data-summary-label="6 positions" selected>'
        '6 positions — recommended</option>'
    ) in html
    assert '<option value="5" selected>' not in html
    assert "MMM averaging" not in html
    assert "Assertive" not in html
    assert 'id="repeat-main-position"' not in html
    assert (
        '<p id="repeat-main-position-disclosure" class="hint"></p>'
        in html
    )
    assert "automatically repeats the main-seat measurement once" not in html
    assert html.index('id="repeat-main-position-disclosure"') < html.index(
        'id="measurement-options" class="hidden"'
    )
    assert "house-curve tilt" not in html
    assert "PEQ policy" not in html
    assert "WebKit Bug" not in html
    assert "Safari" not in html
    assert "RMS:" not in html
    assert "dBFS" not in html
    assert "1 kHz" not in html
    assert "software volume" not in html
    assert "amplifier gain" not in html
    assert "analog gain" not in html
    assert "preference EQ" not in html
    assert "raw room" not in html


def test_browser_has_no_screen_visibility_or_forward_action_policy_mirror():
    js = _module_js()
    assert "SCREEN_SECTIONS" not in js
    assert "WIZARD_FORWARD_ACTION_BY_STATE" not in js
    assert "wizardProvidesForwardAction" not in js
    assert "showScreenSections" not in js
    assert "SUPPORTED_ENVELOPE_SCHEMA = 9" in js


def test_browser_failure_presentation_matches_server_catalog():
    import json
    import re

    from jasper.correction import failures

    js = _module_js()
    block = re.search(
        r"var KNOWN_FAILURES = \{(?P<body>.*?)\n  \};",
        js,
        re.DOTALL,
    )
    assert block is not None
    entries = re.findall(
        r'^\s*([a-z][a-z0-9_]*): \{text: ("(?:[^"\\]|\\.)*"), '
        r"retryable: (true|false)\},?$",
        block["body"],
        re.MULTILINE,
    )
    browser = {
        code: (json.loads(text), retryable == "true")
        for code, text, retryable in entries
    }
    server = {
        code: (
            failures.public_failure(code)["text"],
            failures.public_failure(code)["retryable"],
        )
        for code in failures.FAILURE_CODES
    }
    assert browser == server


def test_render_escapes_hostname():
    html = correction_setup._render_page(
        'evil"<x>', csrf_token="tok-correction-123456789012345678901234",
    ).decode("utf-8")
    assert 'evil"<x>' not in html
    assert "&quot;" in html or "&lt;x&gt;" in html


# ---------------------------------------------------------------------------
# Routing — behaviour preserved.
# ---------------------------------------------------------------------------


def _drive(path: str, method: str = "GET", *, headers=None, body: bytes = b""):
    """Construct the wizard's Handler without binding a socket and drive a
    single request through it. Returns the raw response bytes."""
    Handler = correction_setup._make_handler({"hostname": "jts.local"})

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


def test_get_root_renders_html():
    resp = _drive("/")
    assert b"200" in resp.split(b"\r\n", 1)[0]
    assert b"/assets/app.css" in resp
    assert b"/assets/correction/js/main.js" in resp


def test_get_room_subpath_renders_room_html():
    resp = _drive("/room/")
    assert b"200" in resp.split(b"\r\n", 1)[0]
    assert b"/assets/correction/js/main.js" in resp
    assert b"/correction/crossover/" in resp


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


def test_get_bass_subpath_renders_display_page():
    resp = _drive("/bass/")
    assert b"200" in resp.split(b"\r\n", 1)[0]
    assert b"Bass management" in resp  # P5: read-only display, not a placeholder
    assert b"/assets/correction/js/bass/main.js" in resp
    assert b"/correction/room/" in resp  # pointer to the bass-region measurement


def test_get_bass_status_returns_display_json():
    import json

    resp = _drive("/bass/status")
    assert b"200" in resp.split(b"\r\n", 1)[0]
    body = json.loads(resp.split(b"\r\n\r\n", 1)[1])
    assert "configured" in body and "corner_hz" in body


def test_get_healthz_ok():
    resp = _drive("/healthz")
    assert b"200" in resp.split(b"\r\n", 1)[0]
    assert b"ok" in resp


def test_get_entry_status_routes_to_lightweight_handler(monkeypatch):
    import json

    payload = {
        "screen": "idle",
        "state": "idle",
        "readiness_blocker": None,
        "current_correction_presentation": {"tone": "flat"},
    }
    monkeypatch.setattr(
        correction_setup,
        "_handle_entry_status",
        lambda _handler: payload,
    )

    resp = _drive("/entry-status")

    assert b"200" in resp.split(b"\r\n", 1)[0]
    assert json.loads(resp.split(b"\r\n\r\n", 1)[1]) == payload


def test_entry_status_reads_lightweight_entry_facts_without_reports(monkeypatch):
    from jasper.correction import bundles

    presentation = {
        "tone": "flat",
        "message_template": "No JTS room correction is applied.",
        "applied_at_epoch": None,
        "reset_allowed": False,
    }
    session = SimpleNamespace(
        state=SimpleNamespace(value="idle"),
    )
    monkeypatch.setattr(
        correction_setup, "_get_or_create_session", lambda: session,
    )
    monkeypatch.setattr(
        correction_setup,
        "_current_config_presentation",
        lambda sess: ({"kind": "flat"}, presentation)
        if sess is session
        else pytest.fail("unexpected session"),
    )
    monkeypatch.setattr(
        correction_setup,
        "_room_readiness",
        lambda: SimpleNamespace(blocker={"code": "speaker_setup_incomplete"}),
    )
    monkeypatch.setattr(
        bundles,
        "list_bundles",
        lambda *_args, **_kwargs: pytest.fail("entry status scanned reports"),
    )

    assert correction_setup._handle_entry_status(None) == {
        "screen": "idle",
        "state": "idle",
        "readiness_blocker": {"code": "speaker_setup_incomplete"},
        "current_correction_presentation": presentation,
    }


def test_unknown_get_route_404():
    resp = _drive("/nope")
    assert b"404" in resp.split(b"\r\n", 1)[0]


def test_post_without_csrf_is_rejected():
    """Every state-changing POST must fail CSRF before doing any work — the
    resilience guard must survive the restyle."""
    resp = _drive("/start", method="POST", body=b"{}")
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
        "/start", "/next-position", "/repeat-position", "/verify",
        "/test-tone", "/autolevel/start", "/autolevel/lock",
        "/autolevel/cancel", "/upload-noise", "/upload-capture",
        "/local-capture/setup",
        "/calibration/fetch", "/calibration/upload", "/apply", "/reset",
        "/session/delete", "/relay/level-match", "/relay/capture",
        "/relay/verify",
        "/balance/start", "/balance/ramp", "/balance/meter",
        "/balance/lock", "/balance/stop", "/balance/apply",
        "/balance/reset",
        "/sync/start", "/sync/play", "/sync/analyze",
        "/sync/relay-capture", "/sync/apply", "/sync/stop", "/sync/reset",
        "/crossover/level-match", "/crossover/recover-volume",
        "/crossover/region-geometry", "/crossover/candidate",
        "/crossover/apply", "/crossover/restore",
        "/crossover/relay-capture", "/crossover/relay-cancel",
        "/crossover/reset",
        # v2 conductor flow (Wave 5a) — registered unconditionally; each
        # handler refuses fail-closed unless JASPER_CROSSOVER_FLOW=v2.
        "/crossover/v2/session", "/crossover/v2/verify", "/crossover/v2/apply",
        "/crossover/driver-test",
        "/crossover/driver-confirm", "/crossover/driver-abort",
        "/crossover/summed-test", "/crossover/driver-capture-sweep",
        "/crossover/summed-capture-sweep", "/crossover/summed-capture",
        # P6 tuning-LLM routes.
        "/interpret", "/propose", "/propose/apply",
    }
    assert known == correction_setup._POST_ROUTES
    route_reference = (
        Path(__file__).resolve().parents[1] / "docs" / "HANDOFF-correction.md"
    ).read_text(encoding="utf-8")
    inventory = route_reference.split("**Concrete shape (current):**", 1)[1]
    inventory = inventory.split("HTTPS fallback", 1)[0]
    documented_posts = {
        line.split()[1]
        for line in inventory.splitlines()
        if line.startswith("POST /")
    }
    assert documented_posts == known
    for route in sorted(known):
        resp = _drive(route, method="POST", body=b"{}")
        assert b"403" in resp.split(b"\r\n", 1)[0], (
            f"{route} should reach the CSRF guard (403)"
        )

    # Driver evidence requires the relay's signal-bounded quiet crop + repeat
    # state machine. The former raw-WAV single-shot route had no product caller
    # and is deliberately absent rather than implicitly accepting null SNR.
    response = _drive("/crossover/driver-capture", method="POST", body=b"wav")
    assert b"404" in response.split(b"\r\n", 1)[0]


def test_crossover_v2_refusal_is_logged_not_silent(monkeypatch, caplog):
    """W6 finding: a refused v2 session/verify start (CrossoverV2Refused or any
    other precondition ValueError) mapped straight to a 400 with NO journal
    signal — the failed session-start was invisible in journalctl. The 400
    response is correct for the browser; the gap was purely observability."""
    monkeypatch.delenv("JASPER_CAPTURE_RELAY_BASE", raising=False)
    monkeypatch.setattr(
        correction_setup, "guard_mutating_request", lambda handler: True
    )
    caplog.set_level(logging.WARNING, logger=correction_setup.logger.name)

    resp = _drive("/crossover/v2/session", method="POST", body=b"{}")

    assert b"400" in resp.split(b"\r\n", 1)[0]
    records = [
        r for r in caplog.records
        if r.getMessage().startswith("event=correction.crossover_v2_refused")
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "route=/crossover/v2/session" in message
    assert "phone-mic relay capture is not configured" in message


def test_apply_blocked_status_maps_to_409_with_named_issue(monkeypatch):
    """Finding N (a): a blocked apply must not read as success. Before this
    fix, /crossover/v2/apply always answered 200 regardless of payload
    contents — a household's browser had no signal that tapping Apply
    silently did nothing (run6-apply-blocked.log: 200 OK on every attempt)."""
    from jasper.web import correction_crossover_v2 as v2host_mod

    monkeypatch.setattr(
        correction_setup, "guard_mutating_request", lambda handler: True
    )
    monkeypatch.setattr(
        v2host_mod,
        "handle_v2_apply",
        lambda raw, run_async, camilla_factory: {
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
        correction_setup, "guard_mutating_request", lambda handler: True
    )
    monkeypatch.setattr(
        v2host_mod,
        "handle_v2_apply",
        lambda raw, run_async, camilla_factory: {"status": "applied", "profile": {}},
    )

    resp = _drive("/crossover/v2/apply", method="POST", body=b"{}")

    assert b"200" in resp.split(b"\r\n", 1)[0]


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
    assert callable(correction_setup._render_page)
    assert callable(correction_setup._make_handler)


def test_service_start_claims_all_crossover_state_owners(monkeypatch):
    from jasper.active_speaker import repeat_admission, web_commissioning
    from jasper.web import correction_crossover_backend

    claims = []
    monkeypatch.setattr(
        repeat_admission, "claim_owner", lambda: claims.append("repeat")
    )
    monkeypatch.setattr(
        correction_crossover_backend,
        "claim_level_run_owner",
        lambda: claims.append("level"),
    )
    monkeypatch.setattr(
        correction_crossover_backend,
        "claim_commissioning_run_owner",
        lambda: claims.append("commissioning"),
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

    correction_setup._claim_crossover_state_owners()

    assert claims == ["repeat", "level", "commissioning", "capture_entry"]


def test_idle_shutdown_invokes_capture_entry_restore(monkeypatch):
    """The idle exit converges an abandoned capture sequence to production.

    The common abandon is the user closing the tab mid-sequence:
    correction-web idles out minutes later, and (being socket-activated) will
    not run again until someone revisits /correction/. Without this hook the
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


def test_main_wires_idle_tracker_to_capture_entry_restore(monkeypatch):
    """main() hands the capture-entry restore to the IdleShutdownTracker."""

    from jasper.web import _systemd

    captured = {}

    class FakeTracker:
        def __init__(self, *_args, **kwargs):
            captured.update(kwargs)

        def start(self):
            pass

    class FakeServer:
        RequestHandlerClass = object

        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(
        correction_setup, "_claim_crossover_state_owners", lambda: None
    )
    monkeypatch.setattr(
        correction_setup, "make_server", lambda *_a, **_kw: FakeServer()
    )
    monkeypatch.setattr(_systemd, "adopt_systemd_sockets", lambda: [])
    monkeypatch.setattr(_systemd, "IdleShutdownTracker", FakeTracker)
    monkeypatch.setattr(_systemd, "install_request_idle_bump", lambda *_a: None)
    monkeypatch.setattr(_systemd, "notify_ready", lambda: None)
    monkeypatch.setattr(_systemd, "notify_stopping", lambda: None)

    assert correction_setup.main(["--host", "127.0.0.1", "--port", "0"]) == 0
    assert (
        captured.get("on_idle_exit")
        is correction_setup._idle_exit_restore_capture_entry
    )


def test_failed_owner_claim_does_not_skip_later_claims(monkeypatch):
    from jasper.active_speaker import repeat_admission
    from jasper.web import correction_crossover_backend

    claims = []

    def fail_repeat():
        raise OSError("repeat state unavailable")

    monkeypatch.setattr(repeat_admission, "claim_owner", fail_repeat)
    monkeypatch.setattr(
        correction_crossover_backend,
        "claim_level_run_owner",
        lambda: claims.append("level"),
    )
    monkeypatch.setattr(
        correction_crossover_backend,
        "claim_commissioning_run_owner",
        lambda: claims.append("commissioning"),
    )

    correction_setup._claim_crossover_state_owners()

    assert claims == ["level", "commissioning"]


def test_comparison_start_publishes_repeat_then_commissioning_authority(monkeypatch):
    from jasper.active_speaker import measurement, repeat_admission
    from jasper.web import correction_crossover_backend

    comparison = {
        "bundle_session_id": "session-1",
        "fingerprint": "a" * 64,
    }
    calls = []
    monkeypatch.setattr(
        repeat_admission,
        "activate",
        lambda value: calls.append(("repeat", value)),
    )
    monkeypatch.setattr(
        correction_crossover_backend,
        "begin_commissioning_run",
        lambda value: calls.append(("commissioning", value)),
    )
    monkeypatch.setattr(
        measurement,
        "clear_active_comparison_set",
        lambda _topology: calls.append(("clear", None)),
    )
    monkeypatch.setattr(
        repeat_admission,
        "invalidate",
        lambda: calls.append(("invalidate", None)),
    )

    correction_setup._activate_crossover_comparison_authorities(
        object(), comparison
    )

    assert calls == [("repeat", comparison), ("commissioning", comparison)]


def test_comparison_start_without_bundle_keeps_lifecycle_unstarted(monkeypatch):
    from jasper.active_speaker import repeat_admission
    from jasper.web import correction_crossover_backend

    comparison = {"bundle_session_id": None, "fingerprint": "a" * 64}
    calls = []
    monkeypatch.setattr(
        repeat_admission,
        "activate",
        lambda value: calls.append(("repeat", value)),
    )
    monkeypatch.setattr(
        correction_crossover_backend,
        "begin_commissioning_run",
        lambda _value: calls.append(("commissioning", None)),
    )

    correction_setup._activate_crossover_comparison_authorities(
        object(), comparison
    )

    assert calls == [("repeat", comparison)]


def test_commissioning_run_start_failure_revokes_comparison_authority(monkeypatch):
    from jasper.active_speaker import measurement, repeat_admission
    from jasper.web import correction_crossover_backend

    comparison = {
        "bundle_session_id": "session-1",
        "fingerprint": "a" * 64,
    }
    calls = []
    topology = object()
    monkeypatch.setattr(
        repeat_admission,
        "activate",
        lambda _value: calls.append("repeat"),
    )
    monkeypatch.setattr(
        correction_crossover_backend,
        "begin_commissioning_run",
        lambda _value: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        measurement,
        "clear_active_comparison_set",
        lambda value: calls.append("clear") if value is topology else None,
    )
    monkeypatch.setattr(
        repeat_admission,
        "invalidate",
        lambda: calls.append("invalidate"),
    )

    with pytest.raises(OSError, match="disk full"):
        correction_setup._activate_crossover_comparison_authorities(
            topology, comparison
        )

    assert calls == ["repeat", "clear", "invalidate"]


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

    monkeypatch.setattr(correction_setup, "_camilla", lambda: _FakeCam())
    # Resolve target without touching the topology-aware carrier.
    async def resolve(_sess, _cam):
        return Path("/etc/camilladsp/no-room.yml")

    monkeypatch.setattr(correction_setup, "_resolve_reset_target_async", resolve)


def test_maybe_auto_revert_acts_only_on_confirmed_revert(monkeypatch, tmp_path):
    import asyncio

    _patch_no_op_camilla(monkeypatch)

    # Run the async helper on a fresh event loop for the test.
    monkeypatch.setattr(
        correction_setup, "_run_async",
        lambda coro, timeout=None: asyncio.new_event_loop().run_until_complete(
            coro
        ),
    )

    for verdict in ("accept", "surface", "revert_pending_confirm", None):
        sess = _FakeSession(verdict, tmp_path)
        assert correction_setup._maybe_auto_revert(sess) is False
        assert sess.revert_calls == []  # never touched CamillaDSP

    sess = _FakeSession("revert", tmp_path)
    assert correction_setup._maybe_auto_revert(sess) is True
    assert sess.revert_calls == [Path("/etc/camilladsp/no-room.yml")]


def test_maybe_auto_revert_swallows_errors(monkeypatch, tmp_path):
    """A revert failure is logged and returns False — it never 500s the verify
    upload (the correction is left applied for manual undo)."""
    import asyncio

    class _FailSession(_FakeSession):
        async def auto_revert(
            self, camilla_set_config, *, target_config_path=None,
        ):
            raise RuntimeError("camilla rejected the base config")

    _patch_no_op_camilla(monkeypatch)
    monkeypatch.setattr(
        correction_setup, "_run_async",
        lambda coro, timeout=None: asyncio.new_event_loop().run_until_complete(
            coro
        ),
    )
    sess = _FailSession("revert", tmp_path)
    assert correction_setup._maybe_auto_revert(sess) is False


def test_target_config_path_parameter_detection_is_shared_by_reset_and_revert():
    # A function with the kwarg → True.
    async def with_kwarg(cam, *, target_config_path=None):
        return True

    # A function without it and no **kwargs → False.
    async def without_kwarg(cam):
        return True

    # A function with **kwargs → True (forwards through).
    async def with_var_kwargs(cam, **kw):
        return True

    assert correction_setup._accepts_target_config_path(with_kwarg) is True
    assert correction_setup._accepts_target_config_path(without_kwarg) is False
    assert correction_setup._accepts_target_config_path(with_var_kwargs) is True


# ---------------------------------------------------------------------------
# P4 upload-handler wiring — the verify upload that lands "revert" must drive
# the auto-revert (SF pin: removing `auto_reverted = _maybe_auto_revert(sess)`
# from _handle_upload_capture must fail these, not ship).
# ---------------------------------------------------------------------------


def _session_primed_for_confirmed_revert(tmp_path):
    """Real MeasurementSession one verify away from a CONFIRMED regression.

    Runs the real pipeline on the module's background loop (measure a
    near-flat seat, apply, one regressed verify → revert_pending_confirm,
    then arm the confirmatory verify sweep) and returns the session plus the
    regressed verify WAV bytes to upload through the handler.
    """
    from jasper.audio_measurement import sweep

    from .correction_session_fixtures import make_measurement_session
    from .test_correction_session import (
        _measure_one_position,
        _run_verify,
        _synthesize_room_capture,
    )

    sess = make_measurement_session(tmp_path)

    async def _prime():
        async def fake_play(path, **kw):
            pass

        async def fake_camilla(path: str) -> bool:
            return True

        await _measure_one_position(sess, room_gain_db=0.5)
        await sess.apply(fake_camilla)
        await _run_verify(sess, verify_room_gain_db=20.0)
        assert sess.acceptance["verdict"] == "revert_pending_confirm"
        # Arm the confirmatory verify; the handler does the upload.
        await sess.start_verify_sweep(fake_play)

    correction_setup._run_async(_prime(), timeout=60.0)

    sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
    regressed = _synthesize_room_capture(
        sweep_signal, sr, mode_freq_hz=80.0, mode_gain_db=20.0,
    )
    wav_path = tmp_path / "confirm_verify_upload.wav"
    sweep.write_sweep_wav(wav_path, regressed.astype("float32"), sr)
    return sess, wav_path.read_bytes()


class _RecordingCam:
    def __init__(self) -> None:
        self.loads: list[str] = []

    async def set_config_file_path(self, path, *, best_effort=False):
        self.loads.append(str(path))
        return True

    async def get_config_file_path(self, *, best_effort=False):
        return "/etc/camilladsp/outputd-cutover.yml"


def test_upload_handler_runs_auto_revert_on_confirmed_regression(
    tmp_path, monkeypatch,
):
    sess, wav_bytes = _session_primed_for_confirmed_revert(tmp_path)
    cam = _RecordingCam()
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(
        correction_setup, "_read_wav_body", lambda handler: wav_bytes,
    )
    monkeypatch.setattr(correction_setup, "_camilla", lambda: cam)
    async def resolve(_sess, _cam):
        return Path("/tmp/no-room-test.yml")

    monkeypatch.setattr(correction_setup, "_resolve_reset_target_async", resolve)

    resp = correction_setup._handle_upload_capture(object())

    # The upload response is mechanism-only; the envelope owns presentation.
    assert set(resp) == {
        "session_id",
        "state",
        "current_position",
        "total_positions",
        "auto_reverted",
    }
    assert resp["auto_reverted"] is True
    # The revert genuinely ran through the shared reset target.
    assert sess.state.value == "idle"
    assert cam.loads == ["/tmp/no-room-test.yml"]
    assert sess.auto_revert_outcome["result"] == "ok"


def test_upload_handler_auto_revert_failure_still_returns_ok(
    tmp_path, monkeypatch,
):
    """A failed auto-revert never 500s the upload: the response reports
    auto_reverted=false, the correction stays applied (VERIFIED), and the
    envelope says so honestly."""
    sess, wav_bytes = _session_primed_for_confirmed_revert(tmp_path)
    cam = _RecordingCam()
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(
        correction_setup, "_read_wav_body", lambda handler: wav_bytes,
    )
    monkeypatch.setattr(correction_setup, "_camilla", lambda: cam)

    async def _boom(s, c):
        raise RuntimeError("target resolution exploded")

    monkeypatch.setattr(correction_setup, "_resolve_reset_target_async", _boom)

    resp = correction_setup._handle_upload_capture(object())

    assert set(resp) == {
        "session_id",
        "state",
        "current_position",
        "total_positions",
        "auto_reverted",
    }
    assert resp["auto_reverted"] is False
    assert cam.loads == []  # nothing was loaded
    assert sess.state.value == "verified"  # correction still applied
    assert sess.auto_revert_outcome["result"] == "failed"

    # The envelope tells the household the truth: still applied + Reset.
    from jasper.correction.envelope import build_envelope

    env = build_envelope(sess)
    assert "STILL APPLIED" in env["verdict_text"]
    assert "Reset" in env["verdict_text"]


# --- W6.1 Findings D + E2: v2 relay visibility + recover-volume routing ----------


class _CleanSessionVolumePlan:
    """A benign session-volume plan for GET-envelope drives (no drain, no
    recovery) so the lazy-ceiling read + status block stay no-ops."""

    needs_recovery = False

    def stale_active(self, now=None) -> bool:
        return False


def test_crossover_envelope_surfaces_the_v2_relay_slot(monkeypatch):
    """Finding D: /crossover/envelope's relay lookup must match crossover_v2:* —
    it filtered only crossover_sweep:/level_ramp:crossover, so ``relay`` came
    back null during an awaiting-phone v2 session and a page reload lost the tap
    link (and the failure copy never reached the household)."""
    import json

    from jasper.active_speaker.crossover_flow import CROSSOVER_FLOW_ENV
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setenv(CROSSOVER_FLOW_ENV, "v2")
    v2host.set_volume_plan_for_tests(_CleanSessionVolumePlan())
    correction_setup._set_relay_capture({
        "tap_link": "https://capture.test/#s=cap_x",
        "status": "waiting",
        "kind": v2host.V2_RELAY_KIND_SESSION,
    })
    try:
        resp = _drive("/crossover/envelope")
        assert b"200" in resp.split(b"\r\n", 1)[0]
        body = json.loads(resp.split(b"\r\n\r\n", 1)[1])
        assert body["relay"] is not None
        assert body["relay"]["tap_link"] == "https://capture.test/#s=cap_x"
        assert body["relay"]["kind"] == "crossover_v2:session"
    finally:
        correction_setup._set_relay_capture(None)
        v2host.set_volume_plan_for_tests(None)


def test_recover_volume_routes_to_the_v2_plan(monkeypatch):
    """Finding E2: when the v2 conductor owns the unresolved session volume, the
    recover-volume endpoint must drive SessionVolumePlan.recover_unresolved — the
    legacy-lease path 409'd crossover_volume_recovery_not_required (the volume
    holds no lease-unresolved state), leaving the recovery button dead."""
    import json

    from jasper.active_speaker.crossover_flow import CROSSOVER_FLOW_ENV
    from jasper.active_speaker.session_volume_plan import SessionVolumeRestoreResult
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setenv(CROSSOVER_FLOW_ENV, "v2")
    monkeypatch.setattr(
        correction_setup, "guard_mutating_request", lambda handler: True
    )
    drained: list = []

    class _V2Plan:
        needs_recovery = True

        async def recover_unresolved(self, set_v, get_v):
            await set_v(-15.0)
            await get_v()
            drained.append(True)
            return SessionVolumeRestoreResult.EXACT_RESTORED

    v2host.set_volume_plan_for_tests(_V2Plan())

    class _Cam:
        async def set_volume_db(self, db, best_effort=False):
            return True

        async def get_volume_db(self, best_effort=False):
            return -15.0

    monkeypatch.setattr(correction_setup, "_camilla", lambda: _Cam())
    try:
        resp = _drive("/crossover/recover-volume", method="POST", body=b"{}")
        assert b"200" in resp.split(b"\r\n", 1)[0]
        body = json.loads(resp.split(b"\r\n\r\n", 1)[1])
        assert body["status"] == "recovered"
        assert body["recovery"] == "exact_restored"
        assert drained == [True]
    finally:
        v2host.set_volume_plan_for_tests(None)
