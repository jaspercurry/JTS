# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AirPlay's inbound volume intercept: template wiring + the hook's mapping.

shairport-sync fires `deploy/bin/jasper-airplay-volume <db>` on every AirPlay
volume message and once at stream start (ADR-0200). The hook is exercised the
way shairport runs it — as a subprocess against a stub jasper-control — so the
pins cover the real sh/awk arithmetic, not a Python restatement of it.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from jasper.volume_coordinator import AIRPLAY_DB_MAX, AIRPLAY_DB_MIN
from tests.shairport_template_helpers import (
    SHAIRPORT_TEMPLATE,
    template_string_value,
)

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "deploy" / "bin" / "jasper-airplay-volume"
INSTALLED_HOOK_PATH = "/usr/local/sbin/jasper-airplay-volume"


class _Recorder(BaseHTTPRequestHandler):
    posts: list[tuple[str, dict]] = []

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        type(self).posts.append((self.path, json.loads(body)))
        payload = b'{"percent": 0}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


@pytest.fixture
def control_stub():
    """A stub jasper-control that records what the hook posted."""
    _Recorder.posts = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_hook(db: str, *, port: int, runtime_dir: Path) -> list[tuple[str, dict]]:
    subprocess.run(
        ["sh", str(HOOK), db],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "RUNTIME_DIRECTORY": str(runtime_dir),
            "JASPER_CONTROL_PORT": str(port),
        },
        check=True,
        timeout=30,
    )
    return list(_Recorder.posts)


# ---------- the hook's dB → canonical-percent map --------------------------


@pytest.mark.parametrize(
    ("db", "expected_percent"),
    [
        # Endpoints of AirPlay's own range.
        (f"{AIRPLAY_DB_MAX:.6f}", 100),
        (f"{AIRPLAY_DB_MIN:.6f}", 0),
        # shairport formats the float, so the midpoint arrives with decimals.
        ("-15.000000", 50),
        ("-7.500000", 75),
        # Out of band in either direction clamps rather than extrapolating.
        ("5", 100),
        ("-60.000000", 0),
    ],
)
def test_hook_maps_airplay_db_onto_the_canonical_percent_scale(
    db, expected_percent, control_stub, tmp_path,
):
    posts = _run_hook(
        db, port=control_stub.server_port, runtime_dir=tmp_path,
    )

    # `source` is load-bearing, not decoration: it routes the write through
    # `observe_source_volume`. Without it jasper-control treats the caller as
    # authoritative and the active-source gate, the echo window and the
    # measurement hold are all bypassed.
    assert posts == [
        ("/volume/set", {"percent": expected_percent, "source": "airplay"}),
    ]


@pytest.mark.parametrize(
    "argument",
    [
        # AirPlay's mute sentinel. macOS flips -144 <-> 0 around session start
        # and stop, so honouring it would mute the speaker on every connect.
        "-144.000000",
        "-144",
        # Nothing outside the sentinel's neighbourhood is a volume either.
        "-1000",
        # A malformed argument must never read as 0 dB (= full volume) or as
        # 0% (= silence).
        "loud",
        "",
    ],
)
def test_hook_publishes_nothing_for_the_mute_sentinel_or_garbage(
    argument, control_stub, tmp_path,
):
    posts = _run_hook(
        argument, port=control_stub.server_port, runtime_dir=tmp_path,
    )

    assert posts == []


def test_hook_survives_an_unreachable_control_daemon(tmp_path):
    """shairport must never inherit a failure from this hook: a lost volume
    nudge is harmless, a wedged renderer is not."""
    # Port 1 is privileged and unbound — curl fails immediately.
    subprocess.run(
        ["sh", str(HOOK), "-10.000000"],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "RUNTIME_DIRECTORY": str(tmp_path),
            "JASPER_CONTROL_PORT": "1",
        },
        check=True,
        timeout=30,
    )


def test_hook_releases_its_mutex_so_the_next_message_is_not_dropped(
    control_stub, tmp_path,
):
    """The mutex that coalesces a slider-drag burst must not outlive one
    invocation, or the first message after a drag would be the last one the
    speaker ever hears."""
    _run_hook("-30.000000", port=control_stub.server_port, runtime_dir=tmp_path)
    posts = _run_hook(
        f"{AIRPLAY_DB_MAX:.6f}",
        port=control_stub.server_port,
        runtime_dir=tmp_path,
    )

    assert [body["percent"] for _, body in posts] == [0, 100]


def test_hook_coalesces_a_drag_burst_and_still_lands_the_final_value(
    control_stub, tmp_path,
):
    """macOS emits a burst during a slider drag or a held volume key. Posting
    each one would open a coordinator per message on a 1 GB Pi; dropping all
    but the first would leave the speaker at the wrong level. The hook has to
    thin the burst AND finish on the newest value.

    The assertions are shaped to survive a slow runner: a slower box coalesces
    harder (fewer posts), and the mutex holder re-reads the published value
    each pass, so the last post is the last value either way.
    """
    messages = [f"{-30.0 + 30.0 * i / 20.0:.6f}" for i in range(21)]
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "RUNTIME_DIRECTORY": str(tmp_path),
        "JASPER_CONTROL_PORT": str(control_stub.server_port),
    }

    running = []
    for db in messages:
        running.append(subprocess.Popen(["sh", str(HOOK), db], env=env))
        time.sleep(0.05)
    for process in running:
        assert process.wait(timeout=30) == 0

    percents = [body["percent"] for _, body in _Recorder.posts]
    assert percents, "the drag produced no volume write at all"
    assert len(percents) < len(messages)
    assert percents[-1] == 100
    assert percents == sorted(percents)


# ---------- template wiring ------------------------------------------------


def test_template_points_shairport_at_the_installed_hook():
    """The renderer substitutes placeholders only; this value ships as-is, so
    the template and the install step must name the same path."""
    conf = SHAIRPORT_TEMPLATE.read_text(encoding="utf-8")
    installer = (
        REPO / "deploy" / "lib" / "install" / "renderers.sh"
    ).read_text(encoding="utf-8")

    assert (
        template_string_value(conf, "run_this_when_volume_is_set")
        == INSTALLED_HOOK_PATH
    )
    assert INSTALLED_HOOK_PATH in installer


def test_template_leaves_the_lane_at_unity_so_camilla_is_the_only_fader():
    """With shairport applying its own softvol the sender's gain compounds
    with the master and quantizes in the lane's 16 bits. Unity keeps one
    fader, in CamillaDSP's float master."""
    conf = SHAIRPORT_TEMPLATE.read_text(encoding="utf-8")

    assert template_string_value(conf, "ignore_volume_control") == "yes"
