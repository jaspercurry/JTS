# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import types
from pathlib import Path

import pytest

import jasper.camilla as camilla_module
from jasper.camilla import (
    CAMILLA_ATTEMPT_BUDGET_S,
    CAMILLA_OPERATION_TIMEOUT_S,
    CamillaController,
    CamillaUnavailable,
    crossover_controller,
)


class _FakeVolume:
    def __init__(self) -> None:
        self.values: list[float] = []
        self.mutes: list[bool] = []

    def set_main_volume(self, value: float) -> None:
        self.values.append(float(value))

    def set_main_mute(self, value: bool) -> None:
        self.mutes.append(bool(value))


class _FakeClient:
    def __init__(self, active_raw_value: str | None = None) -> None:
        self.volume = _FakeVolume()
        self.config = self
        self.general = self
        self.active_raw_values: list[str] = []
        self.active_raw_value = active_raw_value
        self.queries: list[tuple[str, object]] = []
        self.file_paths: list[str] = []
        self.reload_count = 0

    def set_active_raw(self, value: str) -> None:
        self.active_raw_values.append(value)

    def active_raw(self):
        return self.active_raw_value

    def set_file_path(self, path: str) -> None:
        self.file_paths.append(path)

    def reload(self) -> None:
        self.reload_count += 1

    def query(self, command: str, *, arg=None):
        self.queries.append((command, arg))
        return None


def _controller(fake: _FakeClient, tmp_path: Path | None = None) -> CamillaController:
    cam = CamillaController("127.0.0.1", 1234)
    if tmp_path is not None:
        cam._graph_mutation_lock_path = tmp_path / ".dsp_apply.lock"

    async def call(fn):
        return fn(fake)

    cam._call = call  # type: ignore[method-assign]
    return cam


def test_crossover_controller_defaults_to_camilla2_port(monkeypatch):
    # No env set -> the camilla#2 (endpoint-crossover) defaults: loopback,
    # port 1235 (distinct from camilla#1's 1234).
    monkeypatch.delenv("JASPER_CAMILLA2_HOST", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA2_PORT", raising=False)
    cam = crossover_controller()
    assert isinstance(cam, CamillaController)
    assert cam._host == "127.0.0.1"
    assert cam._port == 1235


def test_crossover_controller_honors_env_overrides(monkeypatch):
    monkeypatch.setenv("JASPER_CAMILLA2_HOST", "10.0.0.9")
    monkeypatch.setenv("JASPER_CAMILLA2_PORT", "1299")
    cam = crossover_controller()
    assert cam._host == "10.0.0.9"
    assert cam._port == 1299


@pytest.mark.asyncio
async def test_set_volume_db_clamps_positive_gain_to_zero():
    fake = _FakeClient()
    cam = _controller(fake)

    assert await cam.set_volume_db(6.0)

    assert fake.volume.values == [0.0]


@pytest.mark.asyncio
async def test_set_volume_db_rejects_non_finite_best_effort():
    fake = _FakeClient()
    cam = _controller(fake)

    assert await cam.set_volume_db(float("nan"), best_effort=True) is False

    assert fake.volume.values == []


@pytest.mark.asyncio
async def test_set_volume_db_rejects_non_finite_strict():
    fake = _FakeClient()
    cam = _controller(fake)

    with pytest.raises(ValueError):
        await cam.set_volume_db(float("inf"))

    assert fake.volume.values == []


@pytest.mark.asyncio
async def test_set_main_mute_forwards_boolean_to_camilla():
    fake = _FakeClient()
    cam = _controller(fake)

    assert await cam.set_main_mute(True)
    assert await cam.set_main_mute(False)

    assert fake.volume.mutes == [True, False]


@pytest.mark.asyncio
async def test_set_active_config_raw_uploads_without_file_path_reload(tmp_path):
    fake = _FakeClient()
    cam = _controller(fake, tmp_path)

    assert await cam.set_active_config_raw("---\nfilters: {}\n")

    assert fake.active_raw_values == ["---\nfilters: {}\n"]
    assert fake.queries == []


@pytest.mark.asyncio
async def test_set_active_config_raw_rejects_empty_config():
    fake = _FakeClient()
    cam = _controller(fake)

    assert await cam.set_active_config_raw("", best_effort=True) is False

    assert fake.active_raw_values == []


@pytest.mark.asyncio
async def test_get_active_config_raw_returns_running_graph_yaml():
    fake = _FakeClient(active_raw_value="---\nfilters: {}\n")
    cam = _controller(fake)

    # Reads the RUNNING graph (active_raw), the read-back counterpart to
    # set_active_config_raw — distinct from the persisted config file path.
    assert await cam.get_active_config_raw() == "---\nfilters: {}\n"


@pytest.mark.asyncio
async def test_get_active_config_raw_none_when_no_active_config():
    fake = _FakeClient(active_raw_value=None)
    cam = _controller(fake)

    assert await cam.get_active_config_raw() is None


@pytest.mark.asyncio
async def test_patch_config_uses_camilla_query_escape_hatch(tmp_path):
    fake = _FakeClient()
    cam = _controller(fake, tmp_path)

    patch = {"filters": {"sound_simple_bass": {"parameters": {"gain": 1.5}}}}

    assert await cam.patch_config(patch)

    assert fake.queries == [("PatchConfig", patch)]


@pytest.mark.asyncio
async def test_all_graph_mutations_enter_the_lowest_admission_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake = _FakeClient()
    cam = _controller(fake, tmp_path)
    sources: list[str] = []

    @contextlib.asynccontextmanager
    async def admit(*, source: str, **_kwargs):
        sources.append(source)
        yield

    monkeypatch.setattr("jasper.dsp_apply.camilla_graph_mutation", admit)

    assert await cam.set_config_file_path(str(tmp_path / "candidate.yml"))
    assert await cam.set_active_config_raw("---\nfilters: {}\n")
    assert await cam.patch_config({"filters": {"gain": {"type": "Gain"}}})
    assert await cam.reload()

    assert sources == [
        "camilla.set_config_file_path",
        "camilla.set_active_config_raw",
        "camilla.patch_config",
        "camilla.reload",
    ]
    assert fake.file_paths == [str(tmp_path / "candidate.yml")]
    assert fake.reload_count == 2


class _FakeWebSocket:
    def __init__(self, timeout: float | None) -> None:
        self.timeout = timeout
        self.abort_count = 0

    def abort(self) -> None:
        self.abort_count += 1


def _install_transport_fakes(
    monkeypatch: pytest.MonkeyPatch,
    client_type: type,
    *,
    initial_timeout: float | None = 17.0,
) -> types.ModuleType:
    websocket = types.ModuleType("websocket")
    websocket.default_timeout = initial_timeout
    websocket.getdefaulttimeout = lambda: websocket.default_timeout
    websocket.setdefaulttimeout = lambda value: setattr(
        websocket, "default_timeout", value,
    )
    camilladsp = types.ModuleType("camilladsp")
    camilladsp.CamillaClient = client_type
    monkeypatch.setitem(sys.modules, "websocket", websocket)
    monkeypatch.setitem(sys.modules, "camilladsp", camilladsp)
    return websocket


def test_pinned_transport_exposes_cross_thread_abort_seam():
    from camilladsp import CamillaClient
    from websocket import WebSocket

    client = CamillaClient("127.0.0.1", 1234)

    assert hasattr(client, "_ws")
    assert callable(WebSocket.abort)


def test_connect_timeout_override_is_serialized_and_globally_restored(monkeypatch):
    first_entered = threading.Event()
    release_first = threading.Event()
    connect_timeouts: list[float | None] = []
    connect_lock = threading.Lock()

    class Client:
        def __init__(self, _host: str, _port: int) -> None:
            self._ws = None

        def connect(self) -> None:
            with connect_lock:
                connect_timeouts.append(websocket.default_timeout)
                ordinal = len(connect_timeouts)
            self._ws = _FakeWebSocket(websocket.default_timeout)
            if ordinal == 1:
                first_entered.set()
                assert release_first.wait(2.0)

    websocket = _install_transport_fakes(monkeypatch, Client)
    first = CamillaController("127.0.0.1", 1234)
    second = CamillaController("127.0.0.1", 1235)
    def ensure(controller: CamillaController) -> None:
        controller._ensure()

    thread_one = threading.Thread(target=ensure, args=(first,))
    thread_two = threading.Thread(target=ensure, args=(second,))
    thread_one.start()
    assert first_entered.wait(2.0)
    thread_two.start()
    thread_two.join(0.05)
    assert thread_two.is_alive()
    release_first.set()
    thread_one.join(2.0)
    thread_two.join(2.0)

    assert connect_timeouts == [
        CAMILLA_OPERATION_TIMEOUT_S,
        CAMILLA_OPERATION_TIMEOUT_S,
    ]
    assert websocket.default_timeout == 17.0
    assert first._client._ws.timeout == CAMILLA_OPERATION_TIMEOUT_S
    assert second._client._ws.timeout == CAMILLA_OPERATION_TIMEOUT_S


def test_connect_failure_restores_global_timeout_and_discards_client(monkeypatch):
    class Client:
        def __init__(self, _host: str, _port: int) -> None:
            self._ws = None

        def connect(self) -> None:
            assert websocket.default_timeout == CAMILLA_OPERATION_TIMEOUT_S
            raise OSError("handshake failed")

    websocket = _install_transport_fakes(monkeypatch, Client)
    controller = CamillaController("127.0.0.1", 1234)

    with pytest.raises(OSError, match="handshake failed"):
        controller._ensure()

    assert controller._client is None
    assert websocket.default_timeout == 17.0


@pytest.mark.asyncio
async def test_silent_recv_uses_socket_timeout_and_keeps_one_retry(monkeypatch):
    clients: list[object] = []

    class Client:
        def __init__(self, _host: str, _port: int) -> None:
            self._ws = None
            clients.append(self)

        def connect(self) -> None:
            self._ws = _FakeWebSocket(websocket.default_timeout)

        def silent_read(self) -> None:
            assert self._ws.timeout == CAMILLA_OPERATION_TIMEOUT_S
            raise TimeoutError("silent recv")

    websocket = _install_transport_fakes(monkeypatch, Client)
    controller = CamillaController("127.0.0.1", 1234)

    with pytest.raises(CamillaUnavailable, match="silent recv"):
        await controller._call(lambda client: client.silent_read())

    assert len(clients) == 2
    assert websocket.default_timeout == 17.0
    assert controller._client is None


@pytest.mark.asyncio
async def test_call_classifies_config_validation_error_as_config_rejected(monkeypatch):
    """W6 hardware run 4 finding J: a healthy CamillaDSP that REJECTED a config
    (e.g. "Use of missing mixer 'split_active_2way'") used to be folded into
    the same ``CamillaUnavailable`` a dead/unreachable daemon raises, so the
    journal logged ``reason=CamillaUnavailable`` while Camilla was up and
    answering. Exercised directly against the REAL
    ``camilladsp.exceptions.ConfigValidationError`` (the pip package is a real
    project dependency, not a fake stand-in) via ``_call``'s own retry/classify
    boundary -- bypassing transport with a stubbed ``_ensure`` the same way
    ``test_wall_budget_aborts_each_attempt_and_bounds_retry`` does above."""
    from camilladsp.exceptions import ConfigValidationError

    from jasper.camilla import CamillaConfigRejected

    controller = CamillaController("127.0.0.1", 1234)
    monkeypatch.setattr(controller, "_ensure", lambda _cancelled=None: object())

    def operation(_client) -> None:
        raise ConfigValidationError(
            message="Use of missing mixer 'split_active_2way'", value=None,
        )

    with pytest.raises(CamillaConfigRejected, match="split_active_2way") as exc_info:
        await controller._call(operation)
    # A CamillaUnavailable subclass: every existing `except CamillaUnavailable`
    # call site keeps catching it unchanged.
    assert isinstance(exc_info.value, CamillaUnavailable)


@pytest.mark.asyncio
async def test_call_still_raises_bare_camilla_unavailable_for_other_errors(monkeypatch):
    """The new classification is SPECIFIC to ConfigValidationError -- an
    unrelated failure (e.g. a genuinely unreachable daemon) still raises the
    bare CamillaUnavailable, not the config-rejected subclass."""
    from jasper.camilla import CamillaConfigRejected

    controller = CamillaController("127.0.0.1", 1234)
    monkeypatch.setattr(controller, "_ensure", lambda _cancelled=None: object())

    def operation(_client) -> None:
        raise OSError("connection reset")

    with pytest.raises(CamillaUnavailable, match="connection reset") as exc_info:
        await controller._call(operation)
    assert not isinstance(exc_info.value, CamillaConfigRejected)


class _BlockingWebSocket(_FakeWebSocket):
    def __init__(
        self,
        release: threading.Event,
        *,
        release_after_aborts: int = 1,
    ) -> None:
        super().__init__(CAMILLA_OPERATION_TIMEOUT_S)
        self._release = release
        self._release_after_aborts = release_after_aborts

    def abort(self) -> None:
        super().abort()
        if self.abort_count >= self._release_after_aborts:
            self._release.set()


@pytest.mark.asyncio
async def test_cancellation_aborts_drains_clears_and_never_retries():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0
    client = types.SimpleNamespace(_ws=_BlockingWebSocket(release))
    controller = CamillaController("127.0.0.1", 1234)
    controller._client = client

    def operation(_client) -> None:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(2.0)
        finished.set()
        raise OSError("aborted recv")

    task = asyncio.create_task(controller._call(operation))
    assert await asyncio.to_thread(started.wait, 2.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set()
    assert client._ws.abort_count == 1
    assert calls == 1
    assert controller._client is None


@pytest.mark.asyncio
async def test_cancellation_cancels_worker_still_queued_in_executor(monkeypatch):
    queued = asyncio.Event()
    release = asyncio.Event()
    invoked = 0

    async def queued_to_thread(fn, *args):
        queued.set()
        await release.wait()
        return fn(*args)

    monkeypatch.setattr(asyncio, "to_thread", queued_to_thread)
    controller = CamillaController("127.0.0.1", 1234)
    controller._client = types.SimpleNamespace(_ws=None)

    def operation(_client) -> None:
        nonlocal invoked
        invoked += 1

    task = asyncio.create_task(controller._call(operation))
    await queued.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    release.set()
    await asyncio.sleep(0)
    assert invoked == 0
    assert controller._client is None


@pytest.mark.asyncio
async def test_cancellation_during_connect_aborts_and_restores_global_timeout(
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    clients: list[object] = []

    class Client:
        def __init__(self, _host: str, _port: int) -> None:
            self._ws = None
            clients.append(self)

        def connect(self) -> None:
            self._ws = _BlockingWebSocket(release)
            started.set()
            assert release.wait(2.0)
            raise OSError("aborted GetVersion")

    websocket = _install_transport_fakes(monkeypatch, Client)
    controller = CamillaController("127.0.0.1", 1234)
    mutations = 0

    def mutate(_client) -> None:
        nonlocal mutations
        mutations += 1

    task = asyncio.create_task(controller._call(mutate))
    assert await asyncio.to_thread(started.wait, 2.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert clients[0]._ws.abort_count == 1
    assert mutations == 0
    assert controller._client is None
    assert websocket.default_timeout == 17.0


@pytest.mark.asyncio
async def test_repeated_cancellation_reaborts_but_still_drains_worker():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    websocket = _BlockingWebSocket(release, release_after_aborts=2)
    client = types.SimpleNamespace(_ws=websocket)
    controller = CamillaController("127.0.0.1", 1234)
    controller._client = client

    def operation(_client) -> None:
        started.set()
        assert release.wait(2.0)
        finished.set()
        raise OSError("aborted recv")

    task = asyncio.create_task(controller._call(operation))
    assert await asyncio.to_thread(started.wait, 2.0)
    task.cancel()
    while websocket.abort_count < 1:
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert websocket.abort_count == 2
    assert finished.is_set()
    assert controller._client is None


@pytest.mark.asyncio
async def test_ordinary_failure_retries_once_with_a_fresh_client(monkeypatch):
    first = object()
    second = object()
    ensured = 0
    called: list[object] = []
    controller = CamillaController("127.0.0.1", 1234)

    def ensure(_cancelled=None):
        nonlocal ensured
        ensured += 1
        client = first if ensured == 1 else second
        controller._client = client
        return client

    def operation(client):
        called.append(client)
        if client is first:
            raise OSError("transient")
        return "recovered"

    monkeypatch.setattr(controller, "_ensure", ensure)

    assert await controller._call(operation) == "recovered"
    assert called == [first, second]
    assert ensured == 2


@pytest.mark.asyncio
async def test_close_disconnects_ephemeral_client_without_reconnect():
    disconnects = 0

    class Client:
        _ws = None

        def disconnect(self) -> None:
            nonlocal disconnects
            disconnects += 1

    controller = CamillaController("127.0.0.1", 1234)
    controller._client = Client()

    await controller.close()
    await controller.close()

    assert disconnects == 1
    assert controller._client is None


@pytest.mark.asyncio
async def test_wall_budget_aborts_each_attempt_and_bounds_retry(monkeypatch):
    monkeypatch.setattr(camilla_module, "CAMILLA_ATTEMPT_BUDGET_S", 0.05)
    clients: list[types.SimpleNamespace] = []
    calls = 0
    controller = CamillaController("127.0.0.1", 1234)

    def ensure(_cancelled=None):
        release = threading.Event()
        client = types.SimpleNamespace(_ws=_BlockingWebSocket(release))
        client.release = release
        clients.append(client)
        controller._client = client
        return client

    def operation(client) -> None:
        nonlocal calls
        calls += 1
        assert client.release.wait(2.0)
        raise OSError("aborted by watchdog")

    monkeypatch.setattr(controller, "_ensure", ensure)
    started = asyncio.get_running_loop().time()
    with pytest.raises(CamillaUnavailable, match="operation exceeded"):
        await controller._call(operation)
    elapsed = asyncio.get_running_loop().time() - started

    assert calls == 2
    assert len(clients) == 2
    assert [client._ws.abort_count for client in clients] == [1, 1]
    assert elapsed < 0.5
    assert CAMILLA_ATTEMPT_BUDGET_S * 2 == 10.0


@pytest.mark.asyncio
async def test_cancelled_connect_queued_on_global_lock_never_runs_mutation(
    monkeypatch,
):
    first_entered = threading.Event()
    release_first = threading.Event()
    connection_count = 0
    connection_lock = threading.Lock()

    class Client:
        def __init__(self, _host: str, _port: int) -> None:
            self._ws = None

        def connect(self) -> None:
            nonlocal connection_count
            with connection_lock:
                connection_count += 1
                ordinal = connection_count
            self._ws = _FakeWebSocket(websocket.default_timeout)
            if ordinal == 1:
                first_entered.set()
                assert release_first.wait(2.0)

    websocket = _install_transport_fakes(monkeypatch, Client)
    first = CamillaController("127.0.0.1", 1234)
    second = CamillaController("127.0.0.1", 1235)
    second_mutations = 0
    first_task = asyncio.create_task(first._call(lambda _client: "first"))
    assert await asyncio.to_thread(first_entered.wait, 2.0)

    def mutate(_client) -> None:
        nonlocal second_mutations
        second_mutations += 1

    second_task = asyncio.create_task(second._call(mutate))
    await asyncio.sleep(0.02)
    second_task.cancel()
    await asyncio.sleep(0.02)
    release_first.set()

    assert await first_task == "first"
    with pytest.raises(asyncio.CancelledError):
        await second_task
    assert second_mutations == 0
    assert connection_count == 1
    assert websocket.default_timeout == 17.0
