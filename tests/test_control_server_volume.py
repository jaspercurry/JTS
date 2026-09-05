# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Route tests for ``jasper.control.handlers.volume``.

/volume/*, /transport/*, /source/* and the ``volume_ops`` helpers they
dispatch through (the Spotify router and the duck-active probe).
"""

from __future__ import annotations

import asyncio
import threading
from http.server import ThreadingHTTPServer

import pytest

from jasper.control import state_aggregate
from jasper.control.server import (
    _make_handler,
    _percent_to_db,
)

from tests.control_server_fixtures import (
    _explicit_passive_output_topology,
    _get,
    _isolate_household_secret,
    _post,
    server_with_coordinator,
)

_IMPORTED_FIXTURES = (
    _explicit_passive_output_topology,
    _isolate_household_secret,
    server_with_coordinator,
)


def test_get_volume_uses_persistence_only_read_path(
    server_with_coordinator,
    monkeypatch,
):
    base, fake = server_with_coordinator

    async def coordinator_construction_is_forbidden(*args, **kwargs):
        raise AssertionError("GET /volume must not construct volume actuators")

    import jasper.control.server as srv_mod
    monkeypatch.setattr(
        srv_mod,
        "_with_coordinator",
        coordinator_construction_is_forbidden,
    )

    status, body = _get(f"{base}/volume")
    assert status == 200
    assert body["percent"] == 60
    assert body["muted"] is False
    assert body["restore_percent"] is None
    # `db` is computed from percent for back-compat
    assert body["db"] == round(_percent_to_db(60), 3)
    assert ("get", None) in fake.calls


def test_volume_adjust_native_delta_percent(server_with_coordinator):
    """Newer clients send delta_percent directly."""
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/adjust", {"delta_percent": 10})
    assert status == 200
    assert body["percent"] == 70
    assert ("adjust", 10) in fake.calls


def test_volume_adjust_clamps_high(server_with_coordinator):
    base, fake = server_with_coordinator
    fake._level = 95
    status, body = _post(f"{base}/volume/adjust", {"delta_percent": 20})
    assert status == 200
    assert body["percent"] == 100


def test_volume_adjust_clamps_low(server_with_coordinator):
    base, fake = server_with_coordinator
    fake._level = 5
    status, body = _post(f"{base}/volume/adjust", {"delta_percent": -30})
    assert status == 200
    assert body["percent"] == 0


def test_volume_set_legacy_db(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/set", {"db": -25.0})
    assert status == 200
    # Under the default floor, -25 dB rounds to 50%.
    assert body["percent"] == 50
    assert ("set", 50) in fake.calls


def test_volume_set_native_percent(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/set", {"percent": 75})
    assert status == 200
    assert body["percent"] == 75
    assert ("set", 75) in fake.calls


def test_volume_set_rejects_active_speaker_setup_block(
    monkeypatch, server_with_coordinator,
):
    import jasper.control.server as srv_mod

    base, fake = server_with_coordinator
    monkeypatch.setattr(
        srv_mod,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {
            "active": True,
            "configured": False,
            "volume_allowed": False,
            "grouping_allowed": False,
            "reason": "baseline_summed_validation_missing",
            "detail": "validate the combined crossover before saving the active profile",
        },
    )

    status, body = _post(f"{base}/volume/set", {"percent": 75})

    assert status == 409
    assert "validate the combined crossover" in body["error"]
    assert body["active_speaker_setup"]["volume_allowed"] is False
    assert all(call[0] != "set" for call in fake.calls)


def test_volume_set_clamps(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _post(f"{base}/volume/set", {"percent": 200})
    assert status == 200
    assert body["percent"] == 100


def test_adjust_missing_field_400(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _post(f"{base}/volume/adjust", {})
    assert status == 400


def test_adjust_non_numeric_400(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _post(f"{base}/volume/adjust", {"delta_percent": "loud"})
    assert status == 400


def test_set_missing_field_400(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _post(f"{base}/volume/set", {})
    assert status == 400


def test_volume_set_with_usbsink_source_routes_to_observe(server_with_coordinator):
    """/volume/set with `source: usbsink` should go through
    observe_source_volume so the coordinator's echo-prevention applies.
    Without `source`, the request is authoritative (set path)."""
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/set",
        {"percent": 42, "source": "usbsink"},
    )
    assert status == 200
    assert body["percent"] == 42
    assert body["observation_applied"] is True
    # observe call recorded, not set.
    assert ("observe", 42) in fake.calls
    assert all(c[0] != "set" for c in fake.calls), \
        f"unexpected set call in {fake.calls}"


def test_volume_set_routes_initial_observation_metadata(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/set",
        {
            "percent": 42,
            "source": "usbsink",
            "observation_initial": True,
        },
    )
    assert status == 200
    assert body["observation_applied"] is True
    assert fake.observation_initials == [True]


def test_volume_set_rejects_non_boolean_initial_metadata(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/set",
        {
            "percent": 42,
            "source": "usbsink",
            "observation_initial": "true",
        },
    )
    assert status == 400
    assert body["error"] == "observation_initial must be a boolean"
    assert all(call[0] != "observe" for call in fake.calls)


def test_volume_set_event_log_level_tracks_state_change(
    server_with_coordinator, caplog,
):
    """event=volume.set stays INFO for anything that actually changes state
    (an authoritative set, or an applied observation) and drops to DEBUG for
    a declined observation — a no-op that the usbsink volume bridge can
    retry for hours (jasper/usbsink/volume_bridge.py), which would otherwise
    spam the journal at INFO for something that changed nothing. Only the
    level changes; the fields (new_pct, source, observation_applied,
    client) stay identical across all three call shapes."""
    import logging

    base, fake = server_with_coordinator

    # Applied observation -> INFO (state changed: the coordinator adopted
    # the observed value).
    with caplog.at_level(logging.DEBUG, logger="jasper.control.server"):
        status, body = _post(
            f"{base}/volume/set", {"percent": 42, "source": "usbsink"},
        )
    assert status == 200
    assert body["observation_applied"] is True
    records = [r for r in caplog.records if "event=volume.set" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    caplog.clear()

    # Declined observation -> DEBUG (no-op: the active-source gate — source
    # inactive — or a recent cross-process write within the persistence echo
    # window — nothing about the speaker's state changed).
    async def _decline(source, percent, *, initial=False):
        return False

    fake.observe_source_volume = _decline
    with caplog.at_level(logging.DEBUG, logger="jasper.control.server"):
        status, body = _post(
            f"{base}/volume/set", {"percent": 43, "source": "usbsink"},
        )
    assert status == 200
    assert body["observation_applied"] is False
    records = [r for r in caplog.records if "event=volume.set" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert "new_pct=" in records[0].getMessage()
    assert "source=usbsink" in records[0].getMessage()
    assert "observation_applied=false" in records[0].getMessage()
    caplog.clear()

    # Authoritative set (no `source`, observation_applied stays None) ->
    # INFO — always a real state change.
    with caplog.at_level(logging.DEBUG, logger="jasper.control.server"):
        status, body = _post(f"{base}/volume/set", {"percent": 50})
    assert status == 200
    records = [r for r in caplog.records if "event=volume.set" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO


def test_volume_set_with_unknown_source_falls_back_to_set(server_with_coordinator):
    """Unknown source names go through the authoritative set path so a
    future client that posts a fresh source name doesn't silently
    no-op. (Defensive: avoid 400ing on a typo.)"""
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/set",
        {"percent": 55, "source": "rotary-future-source"},
    )
    assert status == 200
    assert body["percent"] == 55
    assert ("set", 55) in fake.calls


def test_volume_set_without_source_is_authoritative(server_with_coordinator):
    """Existing remote / voice clients post without `source`; they
    continue to hit the authoritative set path."""
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/set", {"percent": 80})
    assert status == 200
    assert ("set", 80) in fake.calls
    assert all(c[0] != "observe" for c in fake.calls)


def test_volume_mute_toggles_off_then_on(server_with_coordinator):
    """First POST mutes (saves 60% pre-mute, returns 0). Second
    POST unmutes (restores 60%). Public endpoint used by the
    dashboard and any future client; the VK-01 knob click moved
    off this route to /transport/* with the multi-tap rebind."""
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/mute", {})
    assert status == 200
    assert body["percent"] == 0
    assert body["muted"] is True
    assert body["restore_percent"] == 60
    assert ("mute", 60) in fake.calls

    # A fresh read uses the same canonical projection as the mutating response.
    # This is the remote-mute regression: it previously exposed the remembered
    # 60% level and made the landing-page slider jump back up while still muted.
    status, body = _get(f"{base}/volume")
    assert status == 200
    assert body["percent"] == 0
    assert body["muted"] is True
    assert body["restore_percent"] == 60

    status, body = _post(f"{base}/volume/mute", {})
    assert status == 200
    assert body["percent"] == 60
    assert body["muted"] is False
    assert body["restore_percent"] is None
    assert ("unmute", 60) in fake.calls


def test_volume_mute_when_already_silent(server_with_coordinator):
    """Edge: clicking mute on a 0% volume saves 0 as pre-mute, level
    stays 0. Click again restores 0. Doesn't blow up — the knob is
    safe to click when nothing's playing."""
    base, fake = server_with_coordinator
    fake._level = 0
    status, body = _post(f"{base}/volume/mute", {})
    assert status == 200
    assert body["percent"] == 0


def _block_active_speaker_volume(monkeypatch):
    """Force the active-speaker readiness gate into the not-safe state.

    Mirrors test_grouping_set_enable_rejects_active_speaker_setup_block: an
    active speaker whose combined crossover hasn't been validated reports
    `volume_allowed=False`, which `_active_speaker_volume_block` turns into a
    block.
    """
    import jasper.control.server as srv_mod

    monkeypatch.setattr(
        srv_mod,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {
            "active": True,
            "configured": False,
            "volume_allowed": False,
            "grouping_allowed": False,
            "reason": "baseline_summed_validation_missing",
            "detail": "validate the combined crossover before saving the active profile",
        },
    )


def test_volume_adjust_refused_when_active_speaker_not_safe(
    monkeypatch, server_with_coordinator,
):
    # Pins the remote-path readiness gate (C3b-4): while the active speaker is
    # unsafe for volume, /volume/adjust must refuse with 409 BEFORE dispatching
    # any coordinator op — full-range gain on a not-yet-validated crossover is a
    # tweeter-damage risk. Delete the `_active_speaker_volume_block()` guard in
    # `_post_volume_adjust` and this 200s + records ("adjust", ...): the tripwire.
    base, fake = server_with_coordinator
    _block_active_speaker_volume(monkeypatch)

    status, body = _post(f"{base}/volume/adjust", {"delta_percent": 5})

    assert status == 409
    assert "validate the combined crossover" in body["error"]
    assert body["active_speaker_setup"]["volume_allowed"] is False
    assert fake.calls == []  # op never dispatched


def test_volume_mute_refused_when_active_speaker_not_safe(
    monkeypatch, server_with_coordinator,
):
    # Same readiness gate for the mute route (C3b-4): a blocked active speaker
    # refuses /volume/mute with 409 before touching the coordinator. Removing the
    # `_active_speaker_volume_block()` guard in `_post_volume_mute` lets the
    # toggle run (200 + ("mute"|"unmute", ...) recorded): the tripwire.
    base, fake = server_with_coordinator
    _block_active_speaker_volume(monkeypatch)

    status, body = _post(f"{base}/volume/mute", {})

    assert status == 409
    assert "validate the combined crossover" in body["error"]
    assert body["active_speaker_setup"]["volume_allowed"] is False
    assert fake.calls == []  # op never dispatched


# --- /transport/{toggle,next,previous} ---


@pytest.fixture
def server_with_transport_stub(monkeypatch):
    """Mirror server_with_coordinator but stub `_dispatch_transport`
    so we don't need a renderer/Spotify-router stack. Yields
    (base_url, calls) where calls is the list of actions dispatched."""
    calls: list[str] = []

    async def fake_dispatch(action: str) -> dict:
        calls.append(action)
        return {"result": "ok", "action": action}

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_dispatch_transport", fake_dispatch)

    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_transport_toggle_dispatches_toggle(server_with_transport_stub):
    base, calls = server_with_transport_stub
    status, body = _post(f"{base}/transport/toggle", {})
    assert status == 200
    assert calls == ["toggle"]
    assert body["action"] == "toggle"


def test_transport_next_dispatches_next(server_with_transport_stub):
    """Double-tap on the remote / VK-01 lands here."""
    base, calls = server_with_transport_stub
    status, body = _post(f"{base}/transport/next", {})
    assert status == 200
    assert calls == ["next"]
    assert body["action"] == "next"


def test_transport_previous_dispatches_previous(server_with_transport_stub):
    """Triple-tap on the remote / VK-01 lands here."""
    base, calls = server_with_transport_stub
    status, body = _post(f"{base}/transport/previous", {})
    assert status == 200
    assert calls == ["previous"]
    assert body["action"] == "previous"


def test_transport_dispatcher_error_propagates_as_502(monkeypatch):
    """If the renderer/router stack errors mid-dispatch the response is
    502 with the error message in the body — same shape as the
    refactored toggle path."""
    async def fake_dispatch(action: str) -> dict:  # noqa: ARG001
        raise RuntimeError("simulated MPRIS unavailable")

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_dispatch_transport", fake_dispatch)

    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, body = _post(f"{base}/transport/next", {})
        assert status == 502
        assert "simulated MPRIS unavailable" in body.get("error", "")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_transport_dispatcher_error_field_propagates_as_502(monkeypatch):
    """If the dispatcher returns {"error": ...} (e.g. "no playing
    source"), the route surfaces it as 502 — same as the existing
    toggle behaviour, so callers and the accessory bridge receive an
    actionable non-2xx result."""
    async def fake_dispatch(action: str) -> dict:  # noqa: ARG001
        return {"error": "no playing source"}

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_dispatch_transport", fake_dispatch)

    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, body = _post(f"{base}/transport/previous", {})
        assert status == 502
        assert body["error"] == "no playing source"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# --- /source/state + /source/select ---


@pytest.fixture
def server_with_mux_stub(monkeypatch):
    """Stub jasper-mux's UDS command helper so source-selection route
    tests don't require a live daemon socket."""
    calls: list[str] = []
    responses: list[dict] = []

    async def fake_mux_command(cmd: str, **kwargs):  # noqa: ARG001
        calls.append(cmd)
        if responses:
            response = responses.pop(0)
            if response.get("raise") == "missing":
                raise FileNotFoundError("/run/jasper-mux/control.sock")
            return response
        if cmd.startswith("SELECT "):
            selected = cmd.split(" ", 1)[1]
            return {
                "mode": "manual",
                "selected_source": selected,
                "active_source": selected,
                "sources": {
                    "airplay": {"playing": selected == "airplay"},
                    "bluetooth": {"playing": selected == "bluetooth"},
                    "spotify": {"playing": selected == "spotify"},
                    "usbsink": {"playing": selected == "usbsink"},
                },
            }
        return {
            "mode": "auto",
            "selected_source": None,
            "active_source": "airplay",
            "sources": {
                "airplay": {"playing": True},
                "bluetooth": {"playing": False},
                "spotify": {"playing": False},
                "usbsink": {"playing": False},
            },
        }

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_mux_socket_command", fake_mux_command)

    def fake_augment(payload: dict) -> dict:
        for source in payload.get("sources", {}).values():
            source["available"] = True
            source["enabled"] = True
        return payload

    monkeypatch.setattr(srv_mod, "_augment_source_payload", fake_augment)

    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, calls, responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_source_state_proxies_mux_status(server_with_mux_stub):
    base, calls, _ = server_with_mux_stub
    status, body = _get(f"{base}/source/state")

    assert status == 200
    assert calls == ["STATUS"]
    assert body["active_source"] == "airplay"
    assert body["sources"]["airplay"]["playing"] is True


def test_source_select_posts_source_to_mux(server_with_mux_stub):
    base, calls, _ = server_with_mux_stub
    status, body = _post(f"{base}/source/select", {"source": "bluetooth"})

    assert status == 200
    assert calls == ["SELECT bluetooth"]
    assert body["mode"] == "manual"
    assert body["selected_source"] == "bluetooth"


def test_source_select_auto_posts_auto_to_mux(server_with_mux_stub):
    base, calls, _ = server_with_mux_stub
    status, _ = _post(f"{base}/source/select", {"source": "auto"})

    assert status == 200
    assert calls == ["AUTO"]


def test_source_select_rejects_unknown_source(server_with_mux_stub):
    base, calls, _ = server_with_mux_stub
    status, body = _post(f"{base}/source/select", {"source": "cassette"})

    assert status == 400
    assert calls == []
    assert "source must be" in body["error"]


def test_source_state_mux_unreachable_is_503(server_with_mux_stub):
    base, calls, responses = server_with_mux_stub
    responses.append({"raise": "missing"})

    status, body = _get(f"{base}/source/state")

    assert status == 503
    assert calls == ["STATUS"]
    assert "jasper-mux unreachable" in body["error"]


def test_source_payload_adds_sources_wizard_availability(monkeypatch):
    import jasper.web.sources_setup as sources_mod
    from jasper.control.server import _augment_source_payload

    monkeypatch.setattr(sources_mod, "_gather_state", lambda: {
        "airplay": {"available": True, "enabled": True},
        "bluetooth": {"available": False, "enabled": False},
        "spotify_connect": {"available": True, "enabled": True},
        "usbsink": {"available": False, "enabled": False},
    })
    payload = {
        "sources": {
            "airplay": {"playing": False},
            "bluetooth": {"playing": False},
            "spotify": {"playing": True},
            "usbsink": {"playing": False},
        },
    }

    result = _augment_source_payload(payload)

    assert result["sources"]["airplay"]["enabled"] is True
    assert result["sources"]["bluetooth"]["available"] is False
    assert result["sources"]["spotify"]["enabled"] is True
    assert result["sources"]["usbsink"]["available"] is False


def test_source_availability_probe_runs_outside_cache_lock(monkeypatch):
    import jasper.control.server as srv_mod
    import jasper.web.sources_setup as sources_mod

    entered_probe = threading.Event()
    release_probe = threading.Event()
    errors: list[BaseException] = []

    def slow_gather_state():
        entered_probe.set()
        assert release_probe.wait(timeout=2)
        return {
            "airplay": {"available": True, "enabled": True},
            "bluetooth": {"available": False, "enabled": False},
            "spotify_connect": {"available": True, "enabled": True},
            "usbsink": {"available": True, "enabled": True},
        }

    def augment():
        try:
            srv_mod._augment_source_payload(
                {
                    "sources": {
                        "airplay": {},
                        "bluetooth": {},
                        "spotify": {},
                        "usbsink": {},
                    },
                },
            )
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    monkeypatch.setattr(sources_mod, "_gather_state", slow_gather_state)
    state_aggregate._source_availability_cache = None
    worker = threading.Thread(target=augment)
    worker.start()
    assert entered_probe.wait(timeout=2)

    acquired = state_aggregate._source_availability_lock.acquire(timeout=0.2)
    try:
        assert acquired, "source availability probe held the cache lock"
    finally:
        if acquired:
            state_aggregate._source_availability_lock.release()
        release_probe.set()
        worker.join(timeout=2)
        state_aggregate._source_availability_cache = None

    assert not worker.is_alive()
    assert not errors


# --- _make_duck_active_probe (cross-daemon Camilla ownership) ------------
#
# Unit tests for the probe factory consumed by per-request
# VolumeCoordinators. Validates the wire format and the fail-open
# error envelope.


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param(
            {"state": "LISTENING", "duck_active": True}, True,
            id="returns_true_when_voice_reports_ducked",
        ),
        pytest.param(
            {"state": "IDLE", "duck_active": False}, False,
            id="returns_false_when_voice_reports_no_duck",
        ),
        # Fan-in can be actively ducking music while Camilla remains the
        # live master-volume surface; the explicit ownership fact wins.
        pytest.param(
            {
                "state": "LISTENING",
                "duck_active": True,
                "camilla_volume_locked": False,
            },
            False,
            id="prefers_explicit_camilla_lock_over_fanin_duck",
        ),
        # Voice daemon socket doesn't exist (jasper-voice crashed or never
        # started) — probe must fail open so the remote keeps working.
        pytest.param(
            FileNotFoundError("/tmp/unused.sock"), None, id="returns_none_on_uds_missing",
        ),
        # Voice daemon is wedged and doesn't respond within 1s — fail open
        # so the remote doesn't lock up waiting for it.
        pytest.param(asyncio.TimeoutError(), None, id="returns_none_on_timeout"),
        # Older jasper-voice without the duck_active field — treated as
        # unknown (fail-open), same as an unexpected value type below.
        pytest.param({"state": "IDLE"}, None, id="returns_none_when_field_absent"),
        pytest.param(
            {"state": "IDLE", "duck_active": "true"}, None,
            id="returns_none_when_field_wrong_type",
        ),
    ],
)
def test_duck_active_probe(monkeypatch, response, expected):
    import jasper.control.server as srv_mod

    async def fake_command(socket_path, cmd, *, timeout=5.0):
        assert cmd == "STATUS"
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_command)
    probe = srv_mod._make_duck_active_probe("/tmp/unused.sock")
    assert asyncio.run(probe()) is expected


# --- Regression tests for the BuildResult return-shape change ---


def test_make_spotify_router_consumes_build_result_correctly(tmp_path, monkeypatch):
    """Pin the BuildResult shape consumption for control/server.py's
    _build_spotify_router_or_none. Same regression as in mux:
    previously `clients = build_clients(...)` was treated as a dict;
    the change to BuildResult silently broke the volume-coordinator
    wiring."""
    from unittest.mock import patch, MagicMock
    from jasper.control.server import _build_spotify_router_or_none
    from jasper.spotify_router import (
        ACCOUNT_OK, AccountClient, AccountStatus, BuildResult, Router,
    )
    from jasper.accounts import Account

    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "a" * 32)
    monkeypatch.setenv(
        "JASPER_SPOTIFY_ACCOUNTS_PATH", str(tmp_path / "accounts.json"),
    )
    (tmp_path / "accounts.json").write_text(
        '{"accounts": [{"name": "jasper", "cache_path": "/nope"}], '
        '"default": "jasper"}'
    )

    fake_client = AccountClient(
        account=Account(name="jasper", cache_path="/nope"),
        sp=MagicMock(),
    )

    def fake_build_clients(_registry, *, client_id, redirect_uri):
        return BuildResult(
            clients={"jasper": fake_client},
            statuses=[AccountStatus(name="jasper", state=ACCOUNT_OK)],
            default_name="jasper",
        )

    with patch("jasper.spotify_router.build_clients", side_effect=fake_build_clients):
        router = _build_spotify_router_or_none()

    assert isinstance(router, Router)
    assert isinstance(router.clients, dict)
    assert "jasper" in router.clients
    assert router.statuses[0].state == ACCOUNT_OK


def test_make_spotify_router_caches_empty_build_until_account_cache_changes(
    tmp_path, monkeypatch,
):
    """Control builds a per-request coordinator for `/volume` and transport
    requests. If every Spotify account is revoked, repeated dashboard polls
    should not re-hit Spotify's token endpoint until either the short cooldown
    expires or the wizard rewrites an account cache."""
    from unittest.mock import patch
    from jasper.control import volume_ops
    from jasper.control.server import _build_spotify_router_or_none
    from jasper.spotify_router import (
        ACCOUNT_REVOKED, AccountStatus, BuildResult,
    )

    cache_path = tmp_path / "jasper-cache.json"
    cache_path.write_text("revoked-v1")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "a" * 32)
    monkeypatch.setenv(
        "JASPER_SPOTIFY_ACCOUNTS_PATH", str(tmp_path / "accounts.json"),
    )
    monkeypatch.setenv("SPOTIFY_CACHE_PATH", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(volume_ops, "_spotify_empty_router_cache", None)
    (tmp_path / "accounts.json").write_text(
        '{"accounts": [{"name": "jasper", "cache_path": "'
        + str(cache_path)
        + '"}], "default": "jasper"}'
    )
    calls = {"n": 0}

    def fake_build_clients(_registry, *, client_id, redirect_uri):
        calls["n"] += 1
        return BuildResult(
            clients={},
            statuses=[AccountStatus(name="jasper", state=ACCOUNT_REVOKED)],
            default_name="jasper",
        )

    with patch("jasper.spotify_router.build_clients", side_effect=fake_build_clients):
        assert _build_spotify_router_or_none() is None
        assert _build_spotify_router_or_none() is None
        cache_path.write_text("revoked-v2-but-file-changed")
        assert _build_spotify_router_or_none() is None

    assert calls["n"] == 2


async def test_dispatch_transport_reuses_spotify_router_helper(monkeypatch):
    import jasper.control.server as srv_mod
    import jasper.renderer as renderer_mod
    import jasper.tools.transport as transport_mod

    router = object()
    seen = {}

    class FakeRendererClient:
        def __init__(self, **kwargs):
            seen["renderer_kwargs"] = kwargs

    def fake_make_transport_dispatcher(renderer, spotify_router):
        seen["renderer"] = renderer
        seen["spotify_router"] = spotify_router

        async def dispatch(action):
            return {"action": action}

        return dispatch

    monkeypatch.setattr(srv_mod, "_build_spotify_router_or_none", lambda: router)
    monkeypatch.setattr(renderer_mod, "RendererClient", FakeRendererClient)
    monkeypatch.setattr(
        transport_mod,
        "make_transport_dispatcher",
        fake_make_transport_dispatcher,
    )

    result = await srv_mod._dispatch_transport("toggle")

    assert result == {"action": "toggle"}
    assert isinstance(seen["renderer"], FakeRendererClient)
    assert seen["spotify_router"] is router


def test_volume_mute_explicit_set_is_idempotent(server_with_coordinator):
    """{"muted": true} twice stays muted; {"muted": false} when already
    unmuted returns the current level untouched — the explicit-set shape
    voice's distinct mute/unmute intents need (a toggle would invert a
    stale intent)."""
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/mute", {"muted": True})
    assert status == 200 and body["percent"] == 0
    status, body = _post(f"{base}/volume/mute", {"muted": True})
    assert status == 200 and body["percent"] == 0  # still muted, no unmute
    status, body = _post(f"{base}/volume/mute", {"muted": False})
    assert status == 200 and body["percent"] == 60  # restored pre-mute level
    status, body = _post(f"{base}/volume/mute", {"muted": False})
    assert status == 200 and body["percent"] == 60  # unchanged, no re-mute


def test_volume_mute_empty_body_keeps_legacy_toggle(server_with_coordinator):
    """HID accessory clicks post an empty body — the toggle contract is
    load-bearing for them and must survive the explicit-set addition."""
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/mute", {})
    assert status == 200 and body["percent"] == 0      # toggled to muted
    status, body = _post(f"{base}/volume/mute", {})
    assert status == 200 and body["percent"] == 60     # toggled back


def test_volume_mute_non_bool_muted_is_400(server_with_coordinator):
    base, _fake = server_with_coordinator
    status, body = _post(f"{base}/volume/mute", {"muted": "yes"})
    assert status == 400
    assert "boolean" in body["error"]
