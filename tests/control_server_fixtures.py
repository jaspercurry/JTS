# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the ``test_control_server*`` modules.

One HTTP server fixture per stubbed collaborator, the fake volume
coordinator, and the urllib request helpers. The two autouse fixtures
isolate household-secret and output-topology state; every consuming
module imports both.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from jasper.control.server import _make_handler
from jasper.volume_coordinator import VolumeState

def _recording_popen(calls: list[list[str]]):
    """Build the minimal Popen double used by command-dispatch route tests."""

    class RecordingPopen:
        def __init__(self, cmd):
            calls.append(cmd)

    return RecordingPopen


@pytest.fixture(autouse=True)
def _isolate_household_secret(monkeypatch, tmp_path):
    """Point household_credential at a throwaway path for every test here.

    _post_grouping_set adopts (on bond) and CLEARS (on unbond) the household
    secret, so any /grouping/set test that reaches the handler would otherwise
    read/delete the real /var/lib/jasper/household_secret if the suite ran on a
    bonded Pi. Redirect it to a tmp file (absent ⇒ unpaired) so the tests never
    touch real system state; tests that need a specific state call
    `_pair_household`/`_unpair_household`, which override this.
    """
    import jasper.control.household_credential as hc
    monkeypatch.setattr(hc, "SECRET_FILE", str(tmp_path / "household_secret"))


@pytest.fixture(autouse=True)
def _explicit_passive_output_topology(monkeypatch, tmp_path):
    """Give unrelated control-route tests explicit output permission."""

    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    save_output_topology(_full_range_stereo(), topology_path)


class FakeCoordinator:
    """In-memory stand-in. Same async surface as VolumeCoordinator."""

    def __init__(self, level: int = 60) -> None:
        self._level = int(level)
        self._pre_mute_level: int | None = None
        self.observation_initials: list[bool] = []
        self.calls: list[tuple[str, int | None]] = []
        self.fail_next = False

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated coordinator failure")

    def get_listening_level(self) -> int:
        self._maybe_fail()
        self.calls.append(("get", None))
        return self._level

    def get_volume_state(self) -> VolumeState:
        self._maybe_fail()
        self.calls.append(("get", None))
        return VolumeState(self._level, self._pre_mute_level)

    def load_persisted_level(self) -> int:
        return self._level

    def is_muted(self) -> bool:
        return self._pre_mute_level is not None

    async def set_listening_level(self, percent: int) -> int:
        self._maybe_fail()
        target = max(0, min(100, int(percent)))
        self._level = target
        self._pre_mute_level = None
        self.calls.append(("set", target))
        return target

    async def adjust_listening_level(self, delta: int) -> int:
        self._maybe_fail()
        target = max(0, min(100, self._level + int(delta)))
        self._level = target
        self._pre_mute_level = None
        self.calls.append(("adjust", int(delta)))
        return target

    async def mute(self) -> int:
        self._maybe_fail()
        saved = self._pre_mute_level if self._pre_mute_level is not None else self._level
        if self._level > 0 and self._pre_mute_level is None:
            self._pre_mute_level = self._level
        self.calls.append(("mute", saved))
        return saved or 0

    async def unmute(self, fallback_level: int = 50) -> int:
        self._maybe_fail()
        target = self._pre_mute_level if self._pre_mute_level is not None else fallback_level
        self._pre_mute_level = None
        self._level = target
        self.calls.append(("unmute", target))
        return target

    async def set_muted(
        self,
        want_muted: bool,
        *,
        fallback_level: int = 50,
    ) -> VolumeState:
        if want_muted:
            if not self.is_muted():
                await self.mute()
        elif self.is_muted():
            await self.unmute(fallback_level)
        return self.get_volume_state()

    async def toggle_mute(self, *, fallback_level: int = 50) -> VolumeState:
        if self.is_muted():
            await self.unmute(fallback_level)
        else:
            await self.mute()
        return self.get_volume_state()

    async def observe_source_volume(
        self,
        source,
        percent: int,
        *,
        initial: bool = False,
    ) -> bool:
        self._maybe_fail()
        # The real coordinator gates this on whether `source` is the
        # currently active one and on echo windows; the fake just
        # records the call so /volume/set route tests can assert the
        # right path was taken. The fake's `_level` mutation mirrors
        # what would happen in the active-source case so the response
        # body has a sensible value.
        target = max(0, min(100, int(percent)))
        self._level = target
        self.observation_initials.append(initial)
        self.calls.append(("observe", target))
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
def server_with_coordinator(monkeypatch):
    """Start a ThreadingHTTPServer and patch _with_coordinator to use
    the fake. Yields (base_url, fake_coord)."""
    fake = FakeCoordinator(level=60)

    async def fake_with_coordinator(op, **kwargs):  # noqa: ARG001
        return await op(fake)

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_with_coordinator", fake_with_coordinator)
    monkeypatch.setattr(srv_mod, "_read_volume_state", fake.get_volume_state)

    class _NoAirPlayProcess:
        returncode = 1

        async def communicate(self):
            return b"", b""

    async def fake_subprocess_exec(*args, **kwargs):  # noqa: ARG001
        return _NoAirPlayProcess()

    async def fake_mux_status(*args, **kwargs):  # noqa: ARG001
        return None

    monkeypatch.setattr(
        srv_mod.asyncio,
        "create_subprocess_exec",
        fake_subprocess_exec,
    )
    monkeypatch.setattr(srv_mod, "_mux_socket_command", fake_mux_status)

    class FakeHaStatus:
        def snapshot(self):
            raw = os.environ.get("JASPER_TEST_HA_STATUS_JSON", "")
            if raw:
                return json.loads(raw)
            return {
                "configured": False,
                "connected": False,
                "url": "",
                "instance_name": None,
                "version": None,
                "error": None,
            }

    handler = _make_handler(
        "127.0.0.1",
        9,
        "/nonexistent.sock",
        ha_status_cache=FakeHaStatus(),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def server_with_voice_socket(monkeypatch):
    """Server fixture for voice UDS endpoints: stubs out the UDS round-trip
    by monkey-patching _voice_socket_command. Yields (base, responses, received).
    Push dicts onto responses to control the next reply; default {"result":"OK"}."""
    voice_responses: list[dict] = []
    received_cmds: list[str] = []

    async def fake_command(socket_path, cmd, **_kwargs):
        received_cmds.append(cmd)
        return voice_responses.pop(0) if voice_responses else {"result": "OK"}

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_command)

    # Coordinator is also patched — session-only tests don't touch
    # volume routes, but the handler factory still needs the wiring.
    fake_coord = FakeCoordinator(level=60)

    async def fake_with_coordinator(op, **kwargs):  # noqa: ARG001
        return await op(fake_coord)

    monkeypatch.setattr(srv_mod, "_with_coordinator", fake_with_coordinator)

    handler = _make_handler("127.0.0.1", 1234, "/tmp/unused.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, voice_responses, received_cmds
    finally:
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=2)


def _maybe_json(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _get(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, _maybe_json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read() if e.fp else b"")


def _post(
    url: str,
    body: dict | None,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(
        url, data=data, headers=req_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, _maybe_json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read() if e.fp else b"")


def _post_raw(
    url: str,
    data: bytes,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(
        url, data=data, headers=req_headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, _maybe_json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read() if e.fp else b"")


def _grouping_test_setup(monkeypatch, tmp_path):
    """Redirect grouping.env to a tmp file + capture reconciler kicks."""
    import jasper.control.server as srv_mod

    env = tmp_path / "grouping.env"
    popens: list[list[str]] = []

    monkeypatch.setattr(srv_mod, "GROUPING_ENV_FILE", str(env))
    monkeypatch.setattr(srv_mod.subprocess, "Popen", _recording_popen(popens))
    srv_mod._reset_grouping_reconciler_kick_coalescer_for_tests()
    return env, popens
