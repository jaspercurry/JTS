# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.web.wake_corpus_setup.

Covers the RecordingBackend state machine, metadata persistence, the
RecordingTask audio collection logic (with a fake UdpMicCapture), and
the HTTP handler routing. Hardware-free.

Strategy for the async parts: the backend's asyncio loop runs in a
daemon thread; tests start + shutdown the backend explicitly. The
UdpMicCapture used by RecordingTask is lazy-imported via
`from jasper.audio_io import UdpMicCapture`, so tests monkeypatch
`jasper.audio_io.UdpMicCapture` to inject a fake before the
RecordingTask is constructed.
"""
from __future__ import annotations

from collections.abc import Iterator
import asyncio
import json
import shutil
import threading
from email.message import Message
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest

# One constant shared with tests that rely on holding the VALID token
# (e.g. the host-axis 403 pin, whose premise breaks silently if the
# fixture token drifts from what the test sends).
TEST_CSRF_TOKEN = "test-token"

from jasper.wake_corpus import runtime_probe
from jasper.wake_corpus.capture_plan import PlanConformance
from jasper.wake_corpus import recording_backend
from jasper.mics import xvf3800
from jasper.web import wake_corpus_setup


# ---------------------------------------------------------------------------
# Relocated static assets. /wake-corpus/ moved onto the canonical design
# system: the page behaviour now lives in an ES module and the bespoke CSS in
# a page stylesheet, so the HTML render-shape assertions below check whichever
# artifact a given string now lives in (page body vs module vs stylesheet),
# mirroring how the /wifi/ migration relocated its inline JS + tests.
# ---------------------------------------------------------------------------

_ASSETS = Path(__file__).resolve().parents[1] / "deploy" / "assets" / "wake-corpus"
_NODE = shutil.which("node")


def _session_metadata(tmp_path: Path) -> tuple[Path, dict]:
    """Return the one enrollment sidecar written by a single-session test."""
    paths = list((tmp_path / "out" / "metadata").glob("enroll_*.json"))
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text())


def _stub_xvf_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    variant: xvf3800.FirmwareVariant | None = xvf3800.VARIANT_6CH,
    present: bool = True,
    channels: int | None = 6,
) -> None:
    plan = xvf3800.chip_beam_plan_for_variant(variant)
    card = variant.alsa_card_name if variant else xvf3800.ALSA_CARD_NAME
    monkeypatch.setattr(
        xvf3800,
        "detect_runtime_profile",
        lambda: xvf3800.RuntimeProfile(
            present=present,
            variant=variant,
            alsa_card_name=card,
            capture_channels=channels,
            chip_beam_plan=plan,
            reason="test profile",
        ),
    )


def _module_js() -> str:
    return (_ASSETS / "js" / "main.js").read_text()


def _controls_js_path() -> Path:
    return _ASSETS / "js" / "controls.js"


def _controls_js() -> str:
    return _controls_js_path().read_text()


def _page_css() -> str:
    return (_ASSETS / "wake-corpus.css").read_text()


def _allow_capture_plan_conformance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        recording_backend,
        "validate_active_capture_plan",
        lambda plan: PlanConformance(
            ok=True,
            status="ok",
            active_plan_id=str(plan.get("plan_id") or ""),
            expected_plan_id=str(plan.get("plan_id") or ""),
            emitted_legs=list(plan.get("expected_emitted_legs") or []),
        ),
    )


def _block_recording_task_start(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    """Pause RecordingTask.start after the backend reserves its slot.

    This exposes the lifecycle window deterministically without sleeping or
    touching UDP implementation details. The release wait is bounded so a
    failed producer cannot hang the suite.
    """
    entered = threading.Event()
    release = threading.Event()
    original_start = recording_backend.RecordingTask.start

    async def blocking_start(task: recording_backend.RecordingTask) -> None:
        entered.set()
        if not release.wait(timeout=2):
            raise TimeoutError("test did not release RecordingTask.start")
        await original_start(task)

    monkeypatch.setattr(
        recording_backend.RecordingTask,
        "start",
        blocking_start,
    )
    return entered, release


# ---------------------------------------------------------------------------
# Fake UDP capture — yields fixed frames at a fast rate, deterministic.
# ---------------------------------------------------------------------------


class _FakeUdpMicCapture:
    """Async context manager + frames() iterator.

    Matches the public surface UdpMicCapture exposes that
    RecordingTask uses. Each instance produces frames with a fixed
    sample value so tests can assert per-leg routing (the AEC ON
    leg's frames stay distinct from the AEC OFF leg's frames in the
    output WAV bytes).
    """

    # Test hook: tests append (port → sample_value) entries so
    # different ports yield distinguishable audio.
    port_to_value: dict[int, int] = {}

    def __init__(self, host: str = "127.0.0.1", port: int = 9876) -> None:
        self._port = port
        self._value = self.port_to_value.get(port, 0)
        self._closed = False

    async def __aenter__(self) -> "_FakeUdpMicCapture":
        return self

    async def __aexit__(self, *exc) -> None:
        self._closed = True

    async def frames(self):
        # Yield ~1 frame per 5 ms (much faster than real-time so 0.1 s
        # of capture produces ~20 frames).
        while not self._closed:
            yield np.full(1280, self._value, dtype=np.int16)
            await asyncio.sleep(0.005)


@pytest.fixture(autouse=True)
def _patch_udp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the fake into jasper.audio_io so RecordingTask's lazy
    import picks it up."""
    import jasper.audio_io as audio_io
    monkeypatch.setattr(audio_io, "UdpMicCapture", _FakeUdpMicCapture)
    # Reset the per-port value map between tests
    _FakeUdpMicCapture.port_to_value = {}

@pytest.fixture(name="backend")
def _backend_fixture(monkeypatch, tmp_path: Path):
    """Construct + start a backend rooted in a tmp dir, tear down on
    test exit. All 4 leg ports configured — matches the production
    default. Tests that exercise 3-leg mode just don't opt into
    include_raw_mic_0."""
    monkeypatch.setattr(
        runtime_probe,
        "BRIDGE_STATS_PATH",
        tmp_path / "missing_aec_bridge_stats.json",
    )
    _allow_capture_plan_conformance(monkeypatch)
    b = wake_corpus_setup.RecordingBackend(
        output_dir=tmp_path / "out",
        ports={
            "on": 9876,
            "off": 9877,
            "dtln": 9878,
            "raw0": 9879,
            "ref": 9880,
            "usb_raw": 9881,
            "usb_webrtc": 9882,
            "usb_dtln": 9883,
            "chip_aec_150": 9887,
            "chip_aec_210": 9888,
            "xvf_raw0_webrtc_aec3": 9889,
            "xvf_raw0_dtln": 9890,
            **wake_corpus_setup.DEFAULT_AEC3_SWEEP_PORTS,
        },
        max_duration_sec=10.0,  # long enough to not auto-stop during tests
    )
    b.start()
    yield b
    b.shutdown()

@pytest.fixture(name="running_server_port")
def _running_server_port_fixture(backend) -> Iterator[int]:
    """Run one recorder HTTP server and own its complete lifecycle."""
    server = wake_corpus_setup.make_server(
        ("127.0.0.1", 0),
        csrf_token=TEST_CSRF_TOKEN,
        backend=backend,
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _use_tmp_bridge_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    system_env: str = "",
    corpus_env: str = "",
) -> tuple[Path, Path]:
    """Point wake_corpus_setup's bridge env helpers at temp files."""
    system_path = tmp_path / "jasper.env"
    bridge_path = tmp_path / "wake_corpus_bridge.env"
    if system_env:
        system_path.write_text(system_env)
    if corpus_env:
        bridge_path.write_text(corpus_env)
    monkeypatch.setattr(runtime_probe, "SYSTEM_ENV_PATH", system_path)
    monkeypatch.setattr(
        runtime_probe, "BRIDGE_CORPUS_ENV_PATH", bridge_path,
    )
    return system_path, bridge_path

def _mutating_status(
    port: int,
    method: str,
    path: str,
    token: str = "",
    *,
    extra_headers: dict[str, str] | None = None,
) -> int:
    """Issue one POST/DELETE against the fixture-owned live server."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    headers = {"Content-Length": "0"}
    if token:
        headers["X-CSRF-Token"] = token
    if extra_headers:
        headers.update(extra_headers)
    conn.request(method, path, b"", headers)
    try:
        return conn.getresponse().status
    finally:
        conn.close()


class _TrackingReader(BytesIO):
    def __init__(self, body: bytes, *, fail: bool = False) -> None:
        super().__init__(body)
        self.fail = fail
        self.read_calls: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_calls.append(size)
        if self.fail:
            raise OSError("request body read failed")
        return super().read(size)


def _corpus_post_handler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: bytes,
    content_length: int | str,
    read_fails: bool = False,
):
    handler_cls = wake_corpus_setup._make_handler_class(object(), "tok")
    monkeypatch.setattr(
        handler_cls,
        "_post_session",
        lambda *_a, **_k: pytest.fail("invalid request must not dispatch"),
    )
    handler = handler_cls.__new__(handler_cls)
    handler.path = "/api/session"
    handler.headers = Message()
    handler.headers["Content-Length"] = str(content_length)
    handler.headers[wake_corpus_setup.CSRF_HEADER] = "tok"
    handler.rfile = _TrackingReader(body, fail=read_fails)
    handler.wfile = BytesIO()
    handler.client_address = ("127.0.0.1", 0)
    captured: dict[str, int | None] = {"status": None}
    handler.send_response = lambda status, *a, **k: captured.__setitem__(
        "status", int(status),
    )
    handler.send_response_only = handler.send_response
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda: None
    handler.address_string = lambda: "127.0.0.1"
    return handler, captured
def _write_mute(path: Path, muted: bool) -> None:
    path.write_text(f"JASPER_MIC_MUTED={1 if muted else 0}\n")


@pytest.fixture(name="mute_path")
def _mute_path_fixture(tmp_path: Path) -> Path:
    return tmp_path / "mic_mute.env"


@pytest.fixture(name="mute_backend")
def _mute_backend_fixture(monkeypatch, tmp_path: Path, mute_path: Path):
    """Backend wired to a tmp mic_mute.env (same shape as `backend`)."""
    monkeypatch.setattr(
        runtime_probe,
        "BRIDGE_STATS_PATH",
        tmp_path / "missing_aec_bridge_stats.json",
    )
    _allow_capture_plan_conformance(monkeypatch)
    b = wake_corpus_setup.RecordingBackend(
        output_dir=tmp_path / "out",
        ports={"on": 9876, "off": 9877, "dtln": 9878},
        max_duration_sec=10.0,
        mic_mute_path=mute_path,
    )
    b.start()
    yield b
    b.shutdown()
