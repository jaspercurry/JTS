# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Core control-server tests: dispatch, request guards, and lifecycle.

Covers what ``jasper.control.server`` itself owns — the route table and
its install-profile policy, the host/origin/body guards, control-token
and household-credential gating, leader forwarding for a paired
follower, the active-speaker output-safety helpers, and the
ThreadingHTTPServer's own lifecycle. Route bodies are tested beside the
``jasper.control.handlers`` mixin that owns them, in the sibling
``test_control_server_<concern>.py`` modules.
"""

from __future__ import annotations

import json
import logging
import re
import signal
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

import pytest

from jasper.control.server import (
    VOLUME_MAX_DB,
    VOLUME_MIN_DB,
    _active_speaker_level_match_provisional,
    _active_speaker_output_safety_snapshot,
    _control_route_allowed_for_install_profile,
    _make_handler,
)
from jasper.control.volume_ops import _clamp_db, _db_to_percent

from tests.control_server_fixtures import (
    _explicit_passive_output_topology,
    _get,
    _grouping_test_setup,
    _isolate_household_secret,
    _post,
    _post_raw,
    server_with_coordinator,
    server_with_voice_socket,
)

_IMPORTED_FIXTURES = (
    _explicit_passive_output_topology,
    _isolate_household_secret,
    server_with_coordinator,
    server_with_voice_socket,
)


def test_active_speaker_output_safety_snapshot_uses_setup_status(
    monkeypatch,
) -> None:
    import jasper.control.server as srv_mod

    def fake_setup(*, active_config_path=None, **_kwargs):
        assert active_config_path.endswith("active_speaker_staged_startup.yml")
        return {
            "active": True,
            "configured": False,
            "volume_allowed": False,
            "grouping_allowed": False,
            "reason": "active_speaker_commissioning_config_loaded",
            "active_config_path": active_config_path,
            "issues": [],
        }

    monkeypatch.setattr(srv_mod, "read_active_speaker_setup_status", fake_setup)

    payload = _active_speaker_output_safety_snapshot({
        "current": {
            "camilla": {
                "config_path": (
                    "/var/lib/camilladsp/configs/"
                    "active_speaker_staged_startup.yml"
                ),
            },
        },
    })

    assert payload["safety_muted"] is True
    assert payload["reason"] == "active_speaker_commissioning_config_loaded"
    assert payload["active_config_path"].endswith(
        "active_speaker_staged_startup.yml"
    )


def test_active_speaker_output_safety_snapshot_allows_setup_ready(
    monkeypatch,
) -> None:
    import jasper.control.server as srv_mod

    def fake_setup(*, active_config_path=None, **_kwargs):
        return {
            "active": True,
            "configured": True,
            "volume_allowed": True,
            "grouping_allowed": True,
            "reason": None,
            "active_config_path": active_config_path,
            "issues": [],
        }

    monkeypatch.setattr(srv_mod, "read_active_speaker_setup_status", fake_setup)

    payload = _active_speaker_output_safety_snapshot({
        "current": {
            "camilla": {
                "config_path": (
                    "/var/lib/camilladsp/configs/"
                    "active_speaker_baseline.yml"
                ),
            },
        },
    })

    assert payload["safety_muted"] is False
    assert payload["reason"] is None


def test_inactive_unconfigured_topology_still_blocks_volume_and_grouping(
    monkeypatch,
) -> None:
    import jasper.control.server as srv_mod

    blocked = {
        "active": False,
        "configured": False,
        "volume_allowed": False,
        "grouping_allowed": False,
        "reason": "output_topology_unconfigured",
        "detail": "choose and save a speaker layout before using audio",
    }
    monkeypatch.setattr(
        srv_mod,
        "read_active_speaker_setup_status",
        lambda **_kwargs: blocked,
    )

    assert srv_mod._active_speaker_volume_block() is blocked
    grouping, setup = srv_mod._active_speaker_grouping_evaluation()
    assert grouping == {
        "allowed": False,
        "detail": "choose and save a speaker layout before using audio",
    }
    assert setup is blocked


def test_level_match_provisional_none_when_no_applied_baseline() -> None:
    # C3b-3: the value is read from the readiness snapshot the caller already
    # computed, not from a second off-disk open. No applicable active baseline ->
    # None: a passive speaker (no baseline_profile), a non-dict setup, and an
    # active baseline whose candidate is not `applied` (e.g. superseded /
    # not-yet-applied) all return None.
    assert _active_speaker_level_match_provisional(None) is None
    assert _active_speaker_level_match_provisional({"baseline_profile": None}) is None
    assert _active_speaker_level_match_provisional({
        "baseline_profile": {"status": "ready_to_apply", "provisional": True},
    }) is None


def test_level_match_provisional_reads_applied_baseline() -> None:
    assert _active_speaker_level_match_provisional({
        "baseline_profile": {"status": "applied", "provisional": True},
    }) is True
    assert _active_speaker_level_match_provisional({
        "baseline_profile": {"status": "applied", "provisional": False},
    }) is False


def test_level_match_provisional_deduped_from_snapshot_setup(
    tmp_path, monkeypatch,
) -> None:
    # C3b-3 dedup pin: the snapshot's `level_match_provisional` is read from the
    # SAME readiness snapshot it already computed (the single source), not a
    # second disk read. Mutation-check: have `read_active_speaker_setup_status`
    # report an applied+provisional baseline and assert the snapshot surfaces it.
    # Reverting the dedup to a stale second disk read against an absent file
    # would yield None here (it would no longer track the snapshot).
    import jasper.control.server as srv_mod

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE",
        str(tmp_path / "absent_baseline_profile.json"),  # nothing on disk
    )

    def fake_setup(**_kwargs):
        return {
            "active": True,
            "configured": True,
            "volume_allowed": True,
            "grouping_allowed": True,
            "reason": None,
            "baseline_profile": {"status": "applied", "provisional": True},
            "issues": [],
        }

    monkeypatch.setattr(srv_mod, "read_active_speaker_setup_status", fake_setup)

    payload = _active_speaker_output_safety_snapshot({
        "current": {"camilla": {"config_path": "/var/lib/camilladsp/configs/x.yml"}},
    })
    # Tracks the snapshot's baseline_profile, despite the on-disk file being absent.
    assert payload["level_match_provisional"] is True


# --- pure helpers ---


def test_clamp_db_endpoints():
    assert _clamp_db(-100.0) == VOLUME_MIN_DB
    assert _clamp_db(50.0) == VOLUME_MAX_DB
    assert _clamp_db(-10.0) == -10.0


def test_db_to_percent_endpoints():
    assert _db_to_percent(VOLUME_MIN_DB) == 0
    assert _db_to_percent(VOLUME_MAX_DB) == 100
    assert _db_to_percent((VOLUME_MIN_DB + VOLUME_MAX_DB) / 2) == 50


# --- management request guardrails ---


def test_rejects_bad_host_on_get(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _get(f"{base}/healthz", headers={"Host": "evil.example"})
    assert status == 403
    assert body["error"] == "host_not_allowed"


def test_cross_site_get_healthz_is_allowed(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _get(
        f"{base}/healthz",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert status == 200
    assert body == {"ok": True}


def _access_log_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    # log_request's access-log line is always logger.info; filter out other
    # levels (e.g. the WARNING http.reject event) rather than asserting on
    # message text.
    return [r for r in caplog.records if r.levelno == logging.INFO]


@pytest.mark.parametrize(
    ("path", "headers", "expected_status", "expected_info_records"),
    [
        pytest.param("/healthz", None, 200, 0, id="200_healthz_not_logged"),
        pytest.param(
            "/healthz", {"Host": "evil.example"}, 403, 1,
            id="non_200_healthz_still_logged",
        ),
        pytest.param(
            "/system/snapshot", None, 200, 0, id="200_system_snapshot_not_logged",
        ),
        pytest.param(
            "/system/snapshot", {"Host": "evil.example"}, 403, 1,
            id="non_200_system_snapshot_still_logged",
        ),
        pytest.param("/volume", None, 200, 1, id="other_path_still_logged"),
    ],
)
def test_access_log_skips_only_200_healthz(
    server_with_coordinator, caplog, path, headers, expected_status,
    expected_info_records,
):
    # The supervisor's own successful liveness self-poll otherwise fills
    # ~45% of this daemon's idle journal volume (measured on jts4,
    # jts.local, jts3). The dashboard's 5s /system/snapshot poll adds up
    # to ~720 lines/hour per open tab on top of that (see
    # jasper/control/server.py Handler.log_request).
    base, _ = server_with_coordinator
    with caplog.at_level("INFO", logger="jasper.control.server"):
        status, _ = _get(f"{base}{path}", headers=headers)
    assert status == expected_status
    assert len(_access_log_records(caplog)) == expected_info_records


def test_full_profile_allows_every_control_route():
    # Full speakers allow every route.
    for path in ("/state", "/mic", "/cue/play", "/session/start"):
        assert _control_route_allowed_for_install_profile(
            "full", method="GET", path=path,
        )
        assert _control_route_allowed_for_install_profile(
            "full", method="POST", path=path,
        )


def test_legacy_endpoint_token_uses_streambox_route_policy():
    # The removed endpoint tier maps to streambox, so the legacy token gets
    # the streambox route policy (e.g. /state + /source/state allowed).
    assert _control_route_allowed_for_install_profile(
        "endpoint", method="GET", path="/state",
    )
    assert _control_route_allowed_for_install_profile(
        "endpoint", method="GET", path="/source/state",
    )
    assert _control_route_allowed_for_install_profile(
        "endpoint", method="POST", path="/source/select",
    )
    assert not _control_route_allowed_for_install_profile(
        "endpoint", method="GET", path="/mic",
    )


def test_streambox_profile_control_route_policy():
    assert _control_route_allowed_for_install_profile(
        "streambox", method="GET", path="/healthz",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="GET", path="/state",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="GET", path="/source/state",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="GET", path="/system/snapshot",
    )
    assert not _control_route_allowed_for_install_profile(
        "streambox", method="GET", path="/mic",
    )
    assert not _control_route_allowed_for_install_profile(
        "streambox", method="GET", path="/aec",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/volume/set",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/source/select",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/transport/toggle",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/transport/next",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/transport/previous",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/system/audio-quality",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/system/usb-latency",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/system/restart/audio",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/usb-forensics",
    )
    assert not _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/mic/mute",
    )


def test_legacy_endpoint_token_uses_streambox_routes_at_http_layer(
    monkeypatch,
    server_with_coordinator,
):
    # A persisted legacy "endpoint" marker normalizes to streambox, so the
    # HTTP route gate applies the streambox policy: source routes are allowed,
    # voice-brain routes are 404.
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "read_install_profile", lambda: "endpoint")

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/healthz")
    assert status == 200
    assert body == {"ok": True}

    # voice-brain route blocked
    status, _body = _get(f"{base}/mic")
    assert status == 404

    # a streambox-allowed route is not 404 from the route gate
    status, _body = _get(f"{base}/grouping")
    assert status != 404


def _grant_streambox(monkeypatch, *capabilities):
    """Rewrite the streambox row of the pure-data grant table.

    The route gate must follow the GRANT, so the pin drives the grant
    directly instead of restating whatever the shipped table happens to
    say — that table is pinned by tests/test_install_profile_capabilities.py.
    """
    from jasper import install_profile as ip

    monkeypatch.setattr(ip, "PROFILE_CAPABILITIES", {
        **ip.PROFILE_CAPABILITIES,
        ip.STREAMBOX_INSTALL_PROFILE: frozenset(capabilities),
    })


@pytest.mark.parametrize("assistant", [True, False])
@pytest.mark.parametrize("path", [
    "/session/start", "/session/end", "/cue/play", "/system/restart/voice",
])
def test_streambox_assistant_routes_follow_the_assistant_grant(
    monkeypatch, assistant, path,
):
    """A streambox granted Capability.ASSISTANT serves the assistant's own
    surface — the two push-to-talk turn boundaries its paired remote's
    bridge posts, cue playback, and restarting the unit. Without the grant
    they stay off the route table. See ADR-0217."""
    from jasper.install_profile import Capability

    _grant_streambox(monkeypatch, *([Capability.ASSISTANT] if assistant else []))

    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path=path,
    ) is assistant


@pytest.mark.parametrize("method, path", [
    ("GET", "/mic"),
    ("GET", "/aec"),
    ("POST", "/mic/mute"),
    ("POST", "/aec/commission"),
])
def test_streambox_wake_stack_routes_stay_blocked_under_the_assistant_grant(
    monkeypatch, method, path,
):
    """ASSISTANT does not carry the local-mic/wake/AEC stack with it: those
    routes need Capability.WAKE_DETECTION, which no Zero-class board gets."""
    from jasper.install_profile import Capability

    _grant_streambox(monkeypatch, Capability.ASSISTANT)

    assert not _control_route_allowed_for_install_profile(
        "streambox", method=method, path=path,
    )


def test_streambox_serves_a_session_start_over_http(
    monkeypatch,
    server_with_coordinator,
):
    """End to end through the same guard chain the accessory bridge's
    localhost POST takes: the route gate no longer 404s it."""
    import jasper.control.server as srv_mod
    from jasper.install_profile import Capability

    monkeypatch.setattr(srv_mod, "read_install_profile", lambda: "streambox")
    _grant_streambox(monkeypatch, Capability.ASSISTANT)

    base, _ = server_with_coordinator
    status, _body = _post(f"{base}/session/start", {"source": "wiim_remote_2"})
    assert status != 404

    status, _body = _get(f"{base}/mic")
    assert status == 404


def test_cross_site_get_rejects_diagnostics_before_subprocess(
    server_with_coordinator, monkeypatch,
):
    import jasper.control.server as srv_mod

    calls = []

    def fake_run(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("diagnostics should not run")

    monkeypatch.setattr(srv_mod.subprocess, "run", fake_run)

    base, _ = server_with_coordinator
    status, body = _get(
        f"{base}/system/diagnostics",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert status == 403
    assert body["error"] == "cross_site_request"
    assert calls == []


def test_post_allows_same_origin_browser_request(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/adjust",
        {"delta_percent": 1},
        headers={"Origin": base},
    )
    assert status == 200
    assert body["percent"] == 61
    assert ("adjust", 1) in fake.calls


def test_post_rejects_cross_origin_browser_request(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/adjust",
        {"delta_percent": 1},
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403
    assert body["error"] == "origin_not_allowed"
    assert fake.calls == []


def test_post_rejects_cross_site_fetch_metadata_without_origin(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/adjust",
        {"delta_percent": 1},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert status == 403
    assert body["error"] == "cross_site_request"
    assert fake.calls == []


def test_post_rejects_dns_rebinding_host(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/adjust",
        {"delta_percent": 1},
        headers={"Host": "evil.example", "Origin": "http://evil.example"},
    )
    assert status == 403
    assert body["error"] == "host_not_allowed"
    assert fake.calls == []


def test_post_rejects_oversized_body_before_dispatch(server_with_coordinator):
    import jasper.control.server as srv_mod

    base, fake = server_with_coordinator
    payload = b"{" + b'"x":' + b'"' + (b"a" * srv_mod.CONTROL_MAX_POST_BYTES) + b'"}'
    status, body = _post_raw(f"{base}/volume/adjust", payload)
    assert status == 413
    assert body["error"] == "request_body_too_large"
    assert fake.calls == []


def test_split_control_helpers_keep_state_at_owner_modules():
    """The helper split must not grow mutable/config mirrors back in server."""
    import jasper.control.server as srv_mod

    mirrored_names = {
        "OUTPUTD_BASE_CAMILLA_CONFIG",
        "SOURCE_AVAILABILITY_TTL_SEC",
        "_source_availability_cache",
        "_source_availability_lock",
        "_AEC_MODE_FILE",
        "_WAKE_MODEL_FILE",
        "_JASPER_ENV_FILE",
        "_TOGGLE_TO_TOKEN",
        "_aec_bridge_active_impl",
        "_aec_fresh_jasper_env_impl",
        "_aec_full_status",
        "_fresh_jasper_env",
        "_read_audio_quality_state",
        "_read_active_audio_converter",
        "_same_config_path",
        "_server_aec_bridge_active_wrapper",
        "_server_fresh_jasper_env_wrapper",
        "_sound_apply_target",
        "_sound_runtime_status",
        "_sync_aec_module",
        "_sync_source_availability_module",
    }
    assert mirrored_names.isdisjoint(vars(srv_mod))

    # These are intentional host seams, not mirrored state: handlers and
    # _get_state composition replace the whole callable in route-level tests.
    for name in (
        "_augment_source_payload",
        "_get_state",
        "_safe_audio_quality_state",
    ):
        assert callable(getattr(srv_mod, name))


def test_control_route_bodies_stay_partitioned_by_concern() -> None:
    """Keep dispatch/security central while route bodies stay modular."""
    from jasper.control.handlers import (
        AecRoutes,
        GroupingRoutes,
        MeasurementRoutes,
        SystemRoutes,
        VoiceRoutes,
        VolumeRoutes,
    )

    handler = _make_handler(
        "127.0.0.1",
        1234,
        "/nonexistent.sock",
        ha_status_cache=object(),
    )
    assert {"do_GET", "do_POST", "_GET_ROUTES", "_POST_ROUTES"} <= set(
        handler.__dict__,
    )

    concern_mixins = (
        VolumeRoutes,
        VoiceRoutes,
        AecRoutes,
        GroupingRoutes,
        MeasurementRoutes,
        SystemRoutes,
    )
    routed_methods = {
        *handler._GET_ROUTES.values(),
        *handler._POST_ROUTES.values(),
    }
    for method_name in routed_methods:
        assert method_name not in handler.__dict__
        owners = [mixin for mixin in concern_mixins if method_name in mixin.__dict__]
        assert len(owners) == 1, (method_name, owners)


# --- 404 / coordinator-failure ---


def test_unknown_route_404(server_with_coordinator):
    base, _ = server_with_coordinator
    status, _ = _get(f"{base}/nope")
    assert status == 404


def test_coordinator_failure_502(server_with_coordinator):
    base, fake = server_with_coordinator
    fake.fail_next = True
    status, body = _post(f"{base}/volume/adjust", {"delta_percent": -10})
    assert status == 502
    assert "error" in body


# ---------------------------------------------------------------------------
# Audit C2 — systemd watchdog plumbing: the HTTP accept loop must drive
# the Heartbeat progress sentinel so a wedged loop stops the WATCHDOG=1
# pats (Type=notify + WatchdogSec in the unit).
# ---------------------------------------------------------------------------


class _StubHeartbeat:
    def __init__(self):
        self.bumps = 0

    def bump(self):
        self.bumps += 1


def _make_loopback_control_server():
    from jasper.control.server import ControlHTTPServer

    return ControlHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)


def test_control_http_server_sheds_when_worker_cap_is_full():
    from jasper.control.server import ControlHTTPServer

    entered = threading.Event()
    release = threading.Event()

    class _BlockingHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return None

        def do_GET(self):
            entered.set()
            assert release.wait(timeout=2), "test did not release handler"
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ControlHTTPServer(
        ("127.0.0.1", 0),
        _BlockingHandler,
        max_workers=1,
        request_timeout_sec=1.0,
    )
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    first: list[tuple[int, dict]] = []
    try:
        t = threading.Thread(
            target=lambda: first.append(_get(f"{base}/first")),
            daemon=True,
        )
        t.start()
        assert entered.wait(timeout=1)

        status, body = _get(f"{base}/second")
        assert status == 429
        assert body["error"] == "server_overloaded"
        assert body["retry_after"] == 1

        release.set()
        t.join(timeout=2)
        assert first == [(200, {"ok": True})]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=2)


def test_control_http_server_coalesces_overload_logs(caplog):
    from jasper.control.server import ControlHTTPServer

    now = 100.0

    def clock() -> float:
        return now

    server = ControlHTTPServer(
        ("127.0.0.1", 0),
        BaseHTTPRequestHandler,
        overload_log_interval_sec=5.0,
        clock=clock,
    )
    try:
        with caplog.at_level("WARNING", logger="jasper.control.server"):
            server._log_overloaded(("127.0.0.1", 1001))
            server._log_overloaded(("127.0.0.1", 1002))
            server._log_overloaded(("127.0.0.1", 1003))
            now = 105.1
            server._log_overloaded(("127.0.0.1", 1004))
    finally:
        server.server_close()

    messages = [
        record.getMessage()
        for record in caplog.records
        if "event=control.overloaded" in record.getMessage()
    ]
    assert len(messages) == 2
    assert "suppressed=0" in messages[0]
    assert "suppressed=2" in messages[1]


def test_service_actions_bumps_attached_heartbeat():
    server = _make_loopback_control_server()
    try:
        hb = _StubHeartbeat()
        server.heartbeat = hb
        server.service_actions()
        server.service_actions()
        assert hb.bumps == 2
    finally:
        server.server_close()


def test_service_actions_without_heartbeat_is_a_noop():
    """Tests / dev runs construct the server without a heartbeat —
    service_actions must not require one."""
    server = _make_loopback_control_server()
    try:
        server.service_actions()  # must not raise
    finally:
        server.server_close()


def test_serve_forever_loop_drives_heartbeat_bumps():
    """End-to-end plumbing: serve_forever's poll loop (the thing
    WatchdogSec is guarding) is what produces progress bumps — no
    requests needed. A wedged loop therefore stops bumping by
    construction."""
    import threading

    server = _make_loopback_control_server()
    hb = _StubHeartbeat()
    server.heartbeat = hb
    t = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01),
        daemon=True,
    )
    t.start()
    try:
        deadline = time.time() + 2.0
        while hb.bumps < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert hb.bumps >= 3
    finally:
        server.shutdown()
        t.join(timeout=2.0)
        server.server_close()


def test_control_unit_declares_notify_watchdog():
    """The unit file half of C2: Type=notify + WatchdogSec must stay
    paired with the in-process heartbeat (either one alone is broken —
    notify without pings hangs startup; pings without notify are
    ignored)."""
    unit = (REPO_ROOT / "deploy" / "systemd" / "jasper-control.service").read_text()
    assert "Type=notify" in unit
    assert "Type=simple" not in unit
    assert re.search(r"^WatchdogSec=\d+s?$", unit, re.M)


def test_sigterm_handler_requests_shutdown_from_helper_thread():
    import jasper.control.server as srv_mod

    shutdown_seen = threading.Event()
    thread_names: list[str] = []

    class FakeServer:
        def shutdown(self):
            thread_names.append(threading.current_thread().name)
            shutdown_seen.set()

    restore = srv_mod._install_sigterm_shutdown(FakeServer())
    try:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert shutdown_seen.wait(timeout=2)
    finally:
        restore()

    assert thread_names == ["control-sigterm-shutdown"]


def test_stop_peering_daemon_stops_loop_and_runs_daemon_stop(monkeypatch):
    import jasper.control.server as srv_mod
    import jasper.peering as peering_pkg
    import jasper.peering.daemon as peering_daemon_mod

    started = threading.Event()
    stopped = threading.Event()

    class _Mode:
        value = "on"

    class _Config:
        enabled = True
        mode = _Mode()

    class FakePeeringDaemon:
        def __init__(self, cfg):
            self.cfg = cfg

        async def start(self):
            started.set()

        async def stop(self):
            stopped.set()

    monkeypatch.setattr(peering_pkg, "load_config", lambda: _Config())
    monkeypatch.setattr(peering_daemon_mod, "PeeringDaemon", FakePeeringDaemon)
    with srv_mod._peering_lock:
        srv_mod._peering_thread = None
        srv_mod._peering_loop = None
    try:
        srv_mod.start_peering_daemon_if_enabled()
        assert started.wait(timeout=2)
        srv_mod.stop_peering_daemon(timeout=2)
        assert stopped.wait(timeout=2)
        with srv_mod._peering_lock:
            assert srv_mod._peering_thread is None
            assert srv_mod._peering_loop is None
    finally:
        srv_mod.stop_peering_daemon(timeout=1)
        with srv_mod._peering_lock:
            srv_mod._peering_thread = None
            srv_mod._peering_loop = None


# ---------------------------------------------------------------------------
# Bonded-follower volume proxy — /volume* forwards to the pair leader.
# ---------------------------------------------------------------------------


def _grouping_cfg(**kw):
    from jasper.multiroom.config import GroupingConfig
    base = dict(enabled=True, role="follower", channel="right",
                bond_id="bond-1", leader_addr="jts.local", buffer_ms=400,
                codec="flac", error=None)
    base.update(kw)
    return GroupingConfig(**base)


def test_pair_follower_leader_addr_resolution(monkeypatch):
    """Only an ACTIVE bonded follower forwards: leader, solo, and
    fail-LOUD-invalid configs all resolve to None (local handling)."""
    import jasper.multiroom.config as mcfg
    import jasper.multiroom.effective_role as effective_role
    import jasper.control.server as srv_mod

    monkeypatch.setattr(
        effective_role, "read_effective_role_status", lambda: {},
    )

    cases = [
        (_grouping_cfg(), "jts.local"),
        (_grouping_cfg(role="leader", leader_addr=""), None),
        (_grouping_cfg(enabled=False), None),
        (_grouping_cfg(error="broken"), None),
        (_grouping_cfg(leader_addr=""), None),
    ]
    for cfg, want in cases:
        monkeypatch.setattr(mcfg, "load_config", lambda *a, _c=cfg, **k: _c)
        assert srv_mod._pair_follower_leader_addr() == want


def test_refused_follower_landed_solo_does_not_forward_volume(monkeypatch):
    import jasper.control.server as srv_mod
    import jasper.multiroom.config as mcfg
    import jasper.multiroom.effective_role as effective_role

    cfg = _grouping_cfg()
    boot_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(mcfg, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(effective_role, "read_current_boot_id", lambda: boot_id)
    monkeypatch.setattr(
        effective_role,
        "read_effective_role_status",
        lambda: {
            "requested_fingerprint": effective_role.grouping_request_fingerprint(cfg),
            "local_sources_allowed": True,
            "boot_id": boot_id,
        },
    )

    assert srv_mod._pair_follower_leader_addr() is None


class _FakeUpstream:
    """Context-manager response double for urllib.request.urlopen."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


@pytest.fixture
def follower_server(monkeypatch, server_with_coordinator):
    """The coordinator server, with this speaker patched into an active
    bonded follower and the upstream leader call captured."""
    import jasper.control.server as srv_mod

    monkeypatch.setattr(
        srv_mod, "_pair_follower_leader_addr", lambda: "jts.local",
    )
    seen: list = []

    def fake_urlopen(req, timeout=None):
        seen.append((req, timeout))
        return _FakeUpstream(
            b'{"db": -15.0, "percent": 70, "muted": false, '
            b'"restore_percent": null}'
        )

    monkeypatch.setattr(srv_mod, "_pair_urlopen", fake_urlopen)
    base, fake = server_with_coordinator
    return base, fake, seen


def test_follower_get_volume_forwards_to_leader(follower_server):
    base, fake, seen = follower_server
    status, body = _get(f"{base}/volume")
    assert status == 200
    # The leader's payload is relayed, tagged with the pair leader.
    assert body == {
        "db": -15.0,
        "percent": 70,
        "muted": False,
        "restore_percent": None,
        "pair_leader": "jts.local",
    }
    assert fake.calls == []  # the LOCAL coordinator was never touched
    req, timeout = seen[0]
    assert req.full_url.startswith("http://jts.local:")
    assert req.full_url.endswith("/volume")
    assert req.get_header("X-jts-pair-forwarded") == "1"
    assert timeout == 2.5


def test_follower_post_volume_set_relays_body_verbatim(follower_server):
    base, fake, seen = follower_server
    status, body = _post(f"{base}/volume/set", {"percent": 35})
    assert status == 200
    assert body["pair_leader"] == "jts.local"
    assert fake.calls == []
    req, _ = seen[0]
    assert req.full_url.endswith("/volume/set")
    assert json.loads(req.data) == {"percent": 35}


def test_follower_forward_loop_is_broken(follower_server):
    """A request that already carries the forward marker is never forwarded
    again — two mutual followers must error, not ping-pong."""
    base, fake, seen = follower_server
    req = urllib.request.Request(
        f"{base}/volume", headers={"X-JTS-Pair-Forwarded": "1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            status, body = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        status, body = e.code, json.loads(e.read())
    assert status == 502
    assert "loop" in body["error"]
    assert seen == []  # no upstream call attempted


def test_follower_forward_failure_is_502_with_leader_named(
    monkeypatch, server_with_coordinator,
):
    import jasper.control.server as srv_mod

    monkeypatch.setattr(
        srv_mod, "_pair_follower_leader_addr", lambda: "jts.local",
    )

    def exploding_urlopen(req, timeout=None):
        raise OSError("no route to host")

    monkeypatch.setattr(srv_mod, "_pair_urlopen", exploding_urlopen)
    base, fake = server_with_coordinator
    status, body = _get(f"{base}/volume")
    assert status == 502
    assert body["pair_leader"] == "jts.local"
    assert "unreachable" in body["error"]
    assert fake.calls == []


def test_follower_forward_relays_leader_http_verdict(
    monkeypatch, server_with_coordinator,
):
    """A leader that ANSWERS with 4xx/5xx is relayed verbatim (status +
    JSON body, pair_leader-tagged) — never mislabeled 'unreachable'. Only
    transport failures take the 502 path."""
    import io
    import jasper.control.server as srv_mod

    monkeypatch.setattr(
        srv_mod, "_pair_follower_leader_addr", lambda: "jts.local",
    )

    def rejecting_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", hdrs=None,
            fp=io.BytesIO(b'{"error": "percent must be an integer"}'),
        )

    monkeypatch.setattr(srv_mod, "_pair_urlopen", rejecting_urlopen)
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/set", {"percent": "shout"})
    assert status == 400
    assert body == {"error": "percent must be an integer",
                    "pair_leader": "jts.local"}
    assert fake.calls == []  # local coordinator untouched


def test_follower_transport_toggle_forwards_to_leader(follower_server):
    """A remote paired to the follower sends play/pause here; with the
    renderer stack parked the local mux has nothing to toggle — the
    leader owns playback, so transport forwards exactly like volume."""
    base, fake, seen = follower_server
    status, body = _post(f"{base}/transport/toggle", {})
    assert status == 200
    assert body["pair_leader"] == "jts.local"
    req, _ = seen[0]
    assert req.full_url.endswith("/transport/toggle")


def test_follower_source_select_forwards_to_leader(follower_server):
    """A bonded follower's local mux is parked, so source selection targets
    the pair leader instead of producing a local mux-unreachable error."""
    base, fake, seen = follower_server
    status, body = _post(f"{base}/source/select", {"source": "airplay"})
    assert status == 200
    assert body["pair_leader"] == "jts.local"
    assert fake.calls == []
    req, _ = seen[0]
    assert req.full_url.endswith("/source/select")
    assert json.loads(req.data) == {"source": "airplay"}


def test_follower_get_mic_reports_pair_parked_state(follower_server):
    """A bonded follower has no local voice socket by design. GET /mic
    must surface the intentional parked state, not generic offline."""
    base, _fake, _seen = follower_server
    status, body = _get(f"{base}/mic")
    assert status == 200
    assert body["status"] == "parked"
    assert body["reason"] == "bonded_follower"
    assert body["available"] is False
    assert body["muted"] is True
    assert body["pair_leader"] == "jts.local"
    assert "pair leader" in body["message"]


def test_follower_mic_mute_refuses_with_pair_parked_state(follower_server):
    """The UI disables this, but direct clients should still get the same
    pair story instead of a misleading voice_daemon-not-running error."""
    base, _fake, _seen = follower_server
    status, body = _post(f"{base}/mic/mute", {"muted": True})
    assert status == 409
    assert body["status"] == "parked"
    assert body["reason"] == "bonded_follower"
    assert body["available"] is False
    assert body["pair_leader"] == "jts.local"
    assert "pair leader" in body["error"]


# --------------------------------------------------------------------------
# Control-token gate (jasper/control/control_token.py).
#
# The primitive is fail-safe-open with no token file, but production startup
# auto-generates one so the high-impact route set requires a matching
# X-JTS-Token header by default. Ungated routes (/volume*, /healthz, …) are
# never affected.
# --------------------------------------------------------------------------

# Derived from the server's own frozenset so a newly gated route is exercised
# automatically and the two can never drift (the gate covers
# /system/restart/voice + /system/restart/audio). The gate rejects before any
# route dispatch, so iterating restart routes here triggers no side effect.
import jasper.control.server as _srv_mod  # noqa: E402

_GATED_ROUTES = tuple(sorted(_srv_mod._TOKEN_GATED_ROUTES))


def test_grouping_set_stays_in_token_gated_routes():
    """Pin the membership invariant the rest of this section derives from.

    The gate-behavior tests below iterate `_TOKEN_GATED_ROUTES` itself, so they'd stay
    green if `/grouping/set` were dropped — they'd just exercise one fewer route. This
    test fails on that removal: `/grouping/set` MUST remain token-gated. Dropping it would
    silently re-open the multiroom-vs-privilege-separation contradiction (the gate is
    mandatory; someone "fixing" the cross-device grouping fan-out by removing the gate is
    exactly the regression this pins). The household-credential work accepts that
    credential *in addition* to the control token on this route — it never un-gates it.
    """
    assert "/grouping/set" in _srv_mod._TOKEN_GATED_ROUTES
    # The full expected gated set, so adding/removing any route is a
    # deliberate, reviewed change rather than a silent drift.
    assert _srv_mod._TOKEN_GATED_ROUTES == frozenset({
        "/system/poweroff",
        "/system/reboot",
        "/system/restart/voice",
        "/system/restart/audio",
        "/usb-forensics",
        "/mic/mute",
            "/aec/usb-mic",
            "/aec/usb-mic-leg",
            "/aec/enhanced-aec/install",
            "/grouping/set",
        "/aec/firmware/update",
        "/aec/commission",
        "/measurement/hold",
        "/measurement/release",
    })


def test_usb_mic_export_stays_in_token_gated_routes():
    """Live room-audio export is a privacy mutation, not a LAN-open control."""

    assert "/aec/usb-mic" in _srv_mod._TOKEN_GATED_ROUTES
    assert "/aec/usb-mic-leg" in _srv_mod._TOKEN_GATED_ROUTES


def test_aec_commission_stays_in_token_gated_routes():
    """An audible, minutes-long measurement that stops voice/AEC is a
    high-impact mutation, not a LAN-open control."""

    assert "/aec/commission" in _srv_mod._TOKEN_GATED_ROUTES


def _enable_control_token(monkeypatch, tmp_path, token="t0ken-value"):
    """Point control_token at a tmp file containing `token` (gate ENABLED)."""
    import jasper.control.control_token as ct

    path = tmp_path / "control_token"
    path.write_text(token + "\n")
    monkeypatch.setattr(ct, "TOKEN_FILE", str(path))
    return token


def _disable_control_token(monkeypatch, tmp_path):
    """Point control_token at an absent file (primitive fail-safe-open)."""
    import jasper.control.control_token as ct

    monkeypatch.setattr(ct, "TOKEN_FILE", str(tmp_path / "absent"))


def _pair_household(monkeypatch, tmp_path, secret="hh-secret-value"):
    """Point household_credential at a tmp file containing `secret` (PAIRED).

    A paired speaker is the steady state: its /grouping/set requires EITHER the
    control token OR this household credential.
    """
    import jasper.control.household_credential as hc

    path = tmp_path / "household_secret"
    path.write_text(secret + "\n")
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    return secret


def _unpair_household(monkeypatch, tmp_path):
    """Point household_credential at an absent file (NOT yet paired).

    Unpaired ⇒ verify() fail-safe-accepts, so /grouping/set is open — the
    deliberate bootstrap window (the secret is distributed over that very route).
    """
    import jasper.control.household_credential as hc

    monkeypatch.setattr(hc, "SECRET_FILE", str(tmp_path / "absent_household"))


def test_default_off_gated_routes_reach_handlers(
    monkeypatch, tmp_path, server_with_coordinator, server_with_voice_socket,
):
    """Primitive fail-open invariant: with NO token file, none of the gated
    routes return control_token_required — each reaches its real handler
    exactly as before the gate existed. We assert the *gate* is a no-op by
    confirming no 403 control_token_required comes back; the handler's own
    success/failure is covered by the per-route tests elsewhere."""
    base, _ = server_with_coordinator
    _disable_control_token(monkeypatch, tmp_path)
    # _grouping_test_setup also patches subprocess.Popen module-wide with a
    # FakePopen, so the /system/poweroff|reboot calls below hit the fake — no
    # test machine reboots. /grouping/set has a clean 200 path under the
    # fixture; use it to prove the request flows straight through the (off)
    # gate to the handler.
    env, _ = _grouping_test_setup(monkeypatch, tmp_path)
    status, body = _post(f"{base}/grouping/set", {"enabled": False})
    assert status == 200
    assert body.get("error") != "control_token_required"
    # The other gated routes: never the token error when the gate is off.
    for route in ("/system/poweroff", "/system/reboot", "/mic/mute"):
        status, body = _post(f"{base}{route}", {"muted": True})
        assert body.get("error") != "control_token_required", route


def test_enabled_gated_routes_403_without_token(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """With the gate enabled, every gated route 403s control_token_required
    when no X-JTS-Token is sent — including before any side effect runs.

    /grouping/set also consults the household credential, so we PAIR the speaker
    here: a paired speaker rejects a tokenless, householdless call on every gated
    route (an UNPAIRED speaker's /grouping/set is the open bootstrap window,
    pinned separately below)."""
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    _pair_household(monkeypatch, tmp_path)
    for route in _GATED_ROUTES:
        status, body = _post(f"{base}{route}", {"muted": True})
        assert status == 403, route
        assert body["error"] == "control_token_required", route


def test_enabled_gated_routes_403_with_wrong_token(
    monkeypatch, tmp_path, server_with_coordinator,
):
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path, token="correct-horse")
    _pair_household(monkeypatch, tmp_path)  # paired: /grouping/set also gated
    for route in _GATED_ROUTES:
        status, body = _post(
            f"{base}{route}", {"muted": True},
            headers={"X-JTS-Token": "wrong-token"},
        )
        assert status == 403, route
        assert body["error"] == "control_token_required", route


def test_enabled_gated_route_succeeds_with_matching_token(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """The matching token lets the request through to the handler. Uses
    /grouping/set (a clean 200 under the fixture) so success is unambiguous."""
    base, _ = server_with_coordinator
    token = _enable_control_token(monkeypatch, tmp_path)
    env, _ = _grouping_test_setup(monkeypatch, tmp_path)
    status, body = _post(
        f"{base}/grouping/set",
        {"enabled": True, "role": "leader", "channel": "left", "bond_id": "x"},
        headers={"X-JTS-Token": token},
    )
    assert status == 200
    assert body["ok"] is True
    assert "JASPER_GROUPING=on" in env.read_text()


def test_enabled_gate_does_not_affect_ungated_routes(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """With the gate enabled, an UNgated route (/volume/set, and the
    /healthz read) works with no token — the remote's low-impact controls stay
    open by design."""
    base, fake = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    # Ungated POST: no token, still succeeds.
    status, body = _post(f"{base}/volume/set", {"percent": 42})
    assert status == 200
    assert body["percent"] == 42
    # Ungated GET liveness: unaffected.
    status, body = _get(f"{base}/healthz")
    assert status == 200 and body == {"ok": True}


# --------------------------------------------------------------------------
# Household credential on /grouping/set (jasper/control/household_credential.py).
#
# The device-to-device gate: a peer fan-out / autonomous re-group presents the
# household secret as X-JTS-Household, verified against each member's persisted
# copy — NOT the per-device CSRF token a leader can't hold for a follower. The
# gate accepts EITHER on /grouping/set ONLY; every other gated route stays
# control-token-only. Fail-safe (absent ⇒ accept) so the first bond, which
# distributes the secret over this very route, isn't rejected by the gate it
# installs.
# --------------------------------------------------------------------------


def test_grouping_set_accepts_household_credential_without_token(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """A paired member accepts /grouping/set on a valid X-JTS-Household with NO
    X-JTS-Token — the cross-device path. Gate accepts EITHER credential."""
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)  # CSRF gate armed
    secret = _pair_household(monkeypatch, tmp_path)
    env, _ = _grouping_test_setup(monkeypatch, tmp_path)
    status, body = _post(
        f"{base}/grouping/set",
        {"enabled": True, "role": "leader", "channel": "left", "bond_id": "x"},
        headers={"X-JTS-Household": secret},
    )
    assert status == 200
    assert body["ok"] is True
    assert "JASPER_GROUPING=on" in env.read_text()


def test_grouping_set_403_without_either_credential_when_paired(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """Once a household is bonded, a tokenless+householdless caller can no longer
    flip grouping — the whole point of the gate."""
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    _pair_household(monkeypatch, tmp_path)
    status, body = _post(f"{base}/grouping/set", {"enabled": False})
    assert status == 403
    assert body["error"] == "control_token_required"


def test_grouping_set_403_with_wrong_household(
    monkeypatch, tmp_path, server_with_coordinator,
):
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    _pair_household(monkeypatch, tmp_path, secret="correct-household")
    status, body = _post(
        f"{base}/grouping/set", {"enabled": False},
        headers={"X-JTS-Household": "wrong-household"},
    )
    assert status == 403
    assert body["error"] == "control_token_required"


def test_household_credential_not_accepted_on_other_gated_routes(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """Scope: X-JTS-Household authorizes ONLY /grouping/set. The other gated
    routes are browser→own-speaker and stay control-token-only — a household
    bearer must NOT open /mic/mute, poweroff, reboot, or the restart routes."""
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    secret = _pair_household(monkeypatch, tmp_path)
    for route in (
        "/system/poweroff", "/system/reboot", "/mic/mute",
        "/system/restart/voice", "/system/restart/audio",
        "/aec/firmware/update",
    ):
        status, body = _post(
            f"{base}{route}", {"muted": True},
            headers={"X-JTS-Household": secret},
        )
        assert status == 403, route
        assert body["error"] == "control_token_required", route


def test_unbonded_follower_accepts_and_adopts_grouping_fanout(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """BOOTSTRAP regression: an UNPAIRED follower (no secret) must accept the
    secret-distributing bond fan-out — proving the gate the secret installs
    doesn't deadlock the install — and ADOPT it so the next cross-device call
    verifies against it. The CSRF gate is armed; the leader sends only
    X-JTS-Household."""
    import jasper.control.household_credential as hc

    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    secret_path = tmp_path / "household_secret"
    monkeypatch.setattr(hc, "SECRET_FILE", str(secret_path))  # follower UNPAIRED
    env, _ = _grouping_test_setup(monkeypatch, tmp_path)
    assert hc.is_paired() is False
    status, body = _post(
        f"{base}/grouping/set",
        {"enabled": True, "role": "leader", "channel": "left", "bond_id": "x"},
        headers={"X-JTS-Household": "leader-minted-secret"},
    )
    assert status == 200  # fail-safe accept → no bootstrap deadlock
    assert body["ok"] is True
    # ...and the follower ADOPTED the leader's secret (trust-on-first-use).
    assert hc.current() == "leader-minted-secret"
    # Now the gate requires it: a tokenless, householdless call 403s (the
    # bootstrap window has closed for this member).
    status, _ = _post(f"{base}/grouping/set", {"enabled": False})
    assert status == 403


def test_follower_with_deleted_secret_can_be_rebonded(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """RECOVERY regression: a follower whose household_secret was DELETED (the
    2026-05-23 ext4-loss class) must be re-bondable — fail-safe accept on absent,
    then re-adopt. Proves self-heal survives file loss (would be bricked by a
    fail-CLOSED gate)."""
    import jasper.control.household_credential as hc

    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    secret_path = tmp_path / "household_secret"
    secret_path.write_text("original-secret\n")
    monkeypatch.setattr(hc, "SECRET_FILE", str(secret_path))
    env, _ = _grouping_test_setup(monkeypatch, tmp_path)
    secret_path.unlink()  # simulate filesystem loss of the secret
    assert hc.is_paired() is False
    status, body = _post(
        f"{base}/grouping/set",
        {"enabled": True, "role": "leader", "channel": "left", "bond_id": "x"},
        headers={"X-JTS-Household": "fresh-secret"},
    )
    assert status == 200
    assert body["ok"] is True
    assert hc.current() == "fresh-secret"  # re-adopted → re-bonded


def test_unpaired_grouping_set_is_open_bootstrap_window(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """The deliberate, documented trade: on an UNPAIRED speaker /grouping/set is
    fail-safe-OPEN (so the distributing fan-out is never rejected), while the
    OTHER gated routes — which never consult the household credential — stay
    closed under the armed control-token gate.

    Honest framing (control-plane-auth §6): this is NOT only a transient window.
    A never-bonded speaker is unpaired permanently (until its first bond), so its
    /grouping/set is open the whole time — genuinely weaker than the always-armed
    control_token for the unpaired case. Accepted as the trusted-LAN residual and
    unavoidable for TOFU bootstrap; pinned here so a change to that posture is
    deliberate, not silent."""
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    _unpair_household(monkeypatch, tmp_path)
    env, _ = _grouping_test_setup(monkeypatch, tmp_path)
    # Unpaired /grouping/set: open even with no credential at all.
    status, _ = _post(f"{base}/grouping/set", {"enabled": False})
    assert status == 200
    # The other gated routes are NOT loosened by the household fail-safe.
    status, body = _post(f"{base}/mic/mute", {"muted": True})
    assert status == 403
    assert body["error"] == "control_token_required"


def test_unbond_clears_household_secret(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """An unbond (/grouping/set enabled=false) carrying the matching secret
    clears it, so the speaker can later re-pair to a different household."""
    import jasper.control.household_credential as hc

    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    secret = _pair_household(monkeypatch, tmp_path, secret="to-be-cleared")
    _grouping_test_setup(monkeypatch, tmp_path)
    assert hc.is_paired() is True
    status, body = _post(
        f"{base}/grouping/set", {"enabled": False},
        headers={"X-JTS-Household": secret},
    )
    assert status == 200
    assert hc.is_paired() is False  # cleared → re-pairable
