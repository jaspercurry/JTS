# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AirPlay's inbound volume intercept: template wiring + the hook's behaviour.

shairport-sync fires `deploy/bin/jasper-airplay-volume <db>` on every AirPlay
volume message, and `--session-start` before the connect-time volume push
(ADR-0206). The hook is exercised the way shairport runs it — as a subprocess
against a stub jasper-control — so the pins cover the real sh/awk arithmetic
and the real delivery loop, not a Python restatement of them.

The map is pinned through the state file the hook publishes, which is written
before it serialises, so those pins run everywhere. The delivery pins need
`flock(1)` and run on Linux only; CI is the authority.
"""
from __future__ import annotations

import fcntl
import json
import shutil
import subprocess
import sys
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
STATE_NAME = "airplay-volume.pct"

# The exact PATH the hook subprocess gets (see _hook_env below) — resolving
# the skip marker against the AMBIENT pytest PATH instead would find flock
# on a Homebrew Mac (/opt/homebrew/bin), then FAIL rather than skip the six
# gated tests below when the hook itself can't see it on this narrower PATH.
_HOOK_PATH = "/usr/bin:/bin:/usr/local/bin"

_FLOCK = shutil.which("flock", path=_HOOK_PATH)
requires_flock = pytest.mark.skipif(
    _FLOCK is None,
    reason="the hook serialises with flock(1), not on its PATH here",
)


def test_flock_is_available_on_linux():
    """Guards the skip marker above. Without this, a runner that lost
    flock(1) would skip every delivery pin below and still report green."""
    if sys.platform.startswith("linux"):
        assert _FLOCK is not None


def _hook_env(runtime_dir: Path) -> dict[str, str]:
    return {"PATH": _HOOK_PATH, "RUNTIME_DIRECTORY": str(runtime_dir)}


def _hook_argv(*args: str, port: int | None = None) -> list[str]:
    """The hook's argv, with the optional control-base override appended
    when a stub is in play — the seam shairport never uses."""
    argv = ["sh", str(HOOK), *args]
    if port is not None:
        argv.append(f"http://127.0.0.1:{port}")
    return argv


def _run_hook(*args: str, runtime_dir: Path, port: int | None = None) -> None:
    subprocess.run(
        _hook_argv(*args, port=port),
        env=_hook_env(runtime_dir),
        check=True,
        timeout=60,
    )


# ---------- the hook's dB → canonical-percent map --------------------------


@pytest.fixture
def losing_invocation(tmp_path):
    """Hold the hook's lock, so the invocation under test is a burst loser.

    A loser is the right subject for the map: it publishes its value and then
    exits without posting, which is both the fast path and the property the
    coalescer depends on — every message's value reaches the file even when
    only one of them gets to deliver. It also keeps these pins
    platform-neutral, since a loser never needs flock(1) to work.
    """
    handle = (tmp_path / "airplay-volume.lock").open("w")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield tmp_path
    finally:
        handle.close()


@pytest.mark.parametrize(
    ("db", "expected_percent"),
    [
        # Endpoints of AirPlay's own range.
        (f"{AIRPLAY_DB_MAX:.6f}", 100),
        (f"{AIRPLAY_DB_MIN:.6f}", 0),
        # shairport formats the float, so real values arrive with decimals.
        ("-15.000000", 50),
        ("-7.500000", 75),
        # Exactly 50.5 %. Round-half-up is the deliberate rule, so 51.
        ("-14.850000", 51),
        # AirPlay's mute sentinel. It needs no branch of its own: clamping
        # lands it on 0 %, which is the level at which the coordinator asserts
        # Camilla main_mute, so a sender's mute button really does silence the
        # speaker.
        ("-144.000000", 0),
        ("-144", 0),
        # Out of band in either direction clamps rather than extrapolating.
        ("5", 100),
        ("-60.000000", 0),
    ],
)
def test_hook_maps_airplay_db_onto_the_canonical_percent_scale(
    db, expected_percent, losing_invocation,
):
    _run_hook(db, runtime_dir=losing_invocation)

    published = (losing_invocation / STATE_NAME).read_text(encoding="utf-8")
    assert published.strip() == str(expected_percent)


@pytest.mark.parametrize("argument", ["loud", "", "12x", "--", "nan"])
def test_hook_publishes_nothing_for_a_malformed_argument(
    argument, losing_invocation,
):
    """A malformed argument must never read as 0 dB (full volume), and must
    not read as a level at all — the hook has no value, so it publishes
    none."""
    _run_hook(argument, runtime_dir=losing_invocation)

    assert not (losing_invocation / STATE_NAME).exists()


def test_hook_ignores_a_missing_runtime_directory(tmp_path):
    """shairport must never inherit a failure from this hook, including in
    the window before its RuntimeDirectory exists."""
    _run_hook("-10.000000", runtime_dir=tmp_path / "absent")


# ---------- delivery to jasper-control -------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    posts: list[tuple[str, dict]] = []
    # One arrival time per post, same index — how a pin says "immediately".
    times: list[float] = []
    statuses: list[int] = []

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        type(self).times.append(time.monotonic())
        type(self).posts.append((self.path, json.loads(body)))
        status = type(self).statuses.pop(0) if type(self).statuses else 200
        payload = b'{"percent": 0}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


@pytest.fixture
def control_stub():
    """A stub jasper-control that records what the hook posted.

    `statuses` scripts the reply codes it hands back, oldest first; anything
    past the end of that list answers 200.
    """
    _Recorder.posts = []
    _Recorder.times = []
    _Recorder.statuses = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@requires_flock
def test_hook_posts_a_source_attributed_observation(control_stub, tmp_path):
    """`source` is load-bearing, not decoration: it routes the write through
    `observe_source_volume`. Without it jasper-control treats the caller as
    authoritative and the active-source gate, the echo window and the
    measurement hold are all bypassed."""
    _run_hook("-7.500000", runtime_dir=tmp_path, port=control_stub.server_port)

    assert _Recorder.posts == [
        ("/volume/set", {"percent": 75, "source": "airplay"}),
    ]


@requires_flock
def test_session_start_marks_only_the_first_observation_as_initial(
    control_stub, tmp_path,
):
    """shairport fires `run_this_before_play_begins` ahead of the connect-time
    volume push. The session's opening write carries `observation_initial`,
    which the coordinator defers while a mute is latched — so connecting a
    sender no longer clears a mute asserted at the speaker. A later nudge must
    NOT carry it, or volume-up-while-muted would do nothing."""
    _run_hook("--session-start", runtime_dir=tmp_path)
    _run_hook("-30.000000", runtime_dir=tmp_path, port=control_stub.server_port)
    _run_hook("-15.000000", runtime_dir=tmp_path, port=control_stub.server_port)

    assert [body for _, body in _Recorder.posts] == [
        {"percent": 0, "source": "airplay", "observation_initial": True},
        {"percent": 50, "source": "airplay"},
    ]


@requires_flock
def test_a_rejected_write_is_retried_rather_than_recorded_as_delivered(
    control_stub, tmp_path,
):
    """409 is what jasper-control answers while active-speaker output is not
    ready. Recording it as delivered would strand the fader at the old level
    for the rest of the drag."""
    _Recorder.statuses = [409]

    _run_hook("-15.000000", runtime_dir=tmp_path, port=control_stub.server_port)

    percents = [body["percent"] for _, body in _Recorder.posts]
    assert percents[:2] == [50, 50]


@requires_flock
def test_hook_survives_an_unreachable_control_daemon(tmp_path):
    """A lost volume nudge is harmless; a hook that fails back into shairport
    is not. Port 1 is privileged and unbound, so curl fails immediately."""
    _run_hook("-10.000000", runtime_dir=tmp_path, port=1)


@requires_flock
def test_hook_releases_its_lock_so_the_next_message_is_not_dropped(
    control_stub, tmp_path,
):
    """The lock that coalesces a drag must not outlive one invocation, or the
    first message after a drag would be the last one the speaker ever
    hears."""
    _run_hook("-30.000000", runtime_dir=tmp_path, port=control_stub.server_port)
    _run_hook(
        f"{AIRPLAY_DB_MAX:.6f}",
        runtime_dir=tmp_path,
        port=control_stub.server_port,
    )

    assert [body["percent"] for _, body in _Recorder.posts] == [0, 100]


def _db_for(percent: float) -> str:
    """The dB shairport hands the hook for a sender slider at `percent`."""
    db = AIRPLAY_DB_MIN + (AIRPLAY_DB_MAX - AIRPLAY_DB_MIN) * percent / 100
    return f"{db:.6f}"


def _fire_burst(
    percents, *, runtime_dir: Path, port: int, spacing: float,
) -> None:
    """Fire one hook invocation per volume message, the way shairport does.

    Each spawn is scheduled `spacing` after the PREVIOUS one actually
    returned, not against an idealized absolute clock: a starved runner
    that falls behind schedule then stretches the burst instead of firing
    every remaining spawn back-to-back, which would pile on exactly the
    fork/fd pressure it has none left to spare.
    """
    env = _hook_env(runtime_dir)
    running = []
    next_at = time.monotonic()
    for percent in percents:
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        running.append(
            subprocess.Popen(_hook_argv(_db_for(percent), port=port), env=env)
        )
        next_at = time.monotonic() + spacing
    for process in running:
        assert process.wait(timeout=60) == 0


@requires_flock
def test_hook_coalesces_a_drag_burst_and_still_lands_the_final_value(
    control_stub, tmp_path,
):
    """macOS emits a burst during a slider drag or a held volume key. Posting
    each one would build a coordinator per message on a 1 GB Pi; dropping all
    but the first would leave the speaker at the wrong level. The hook has to
    thin the burst AND finish on the newest value.

    The assertions are shaped to survive a slow runner: a slower box coalesces
    harder (fewer posts), and the holder re-reads the published value after
    releasing the lock, so the last post is the last value either way.
    """
    messages = list(range(0, 101, 5))

    _fire_burst(
        messages,
        runtime_dir=tmp_path,
        port=control_stub.server_port,
        spacing=0.05,
    )

    percents = [body["percent"] for _, body in _Recorder.posts]
    assert percents, "the drag produced no volume write at all"
    assert len(percents) < len(messages)
    assert percents[-1] == 100
    assert percents == sorted(percents)


# The connect animation macOS plays at every AirPlay session start, measured
# on jts3: ten volume messages 200 ms apart walking the sender's slider up
# from the bottom of its scale to where the user actually left it (63 %).
CONNECT_FADE_UP = [6, 13, 19, 25, 31, 38, 44, 50, 57, 63]
FADE_SPACING_S = 0.2


@requires_flock
def test_a_session_start_fade_up_is_adopted_once_at_its_settled_level(
    control_stub, tmp_path,
):
    """A session start is not a drag. Replaying the connect animation took the
    master down ~28 dB and swelled it back over two seconds — audible on every
    connect. The speaker adopts the level the animation settles on, once, and
    that write still carries `observation_initial`.

    This is a first-ever session, so there is no remembered level to restore
    ahead of the animation and the settled write is the whole story. The pin
    below covers the session that has one.
    """
    _run_hook("--session-start", runtime_dir=tmp_path)

    _fire_burst(
        CONNECT_FADE_UP,
        runtime_dir=tmp_path,
        port=control_stub.server_port,
        spacing=FADE_SPACING_S,
    )

    assert [body["percent"] for _, body in _Recorder.posts] == [
        CONNECT_FADE_UP[-1],
    ]
    assert _Recorder.posts[0][1].get("observation_initial") is True


@requires_flock
def test_a_reconnect_restores_the_remembered_level_before_the_animation(
    control_stub, tmp_path,
):
    """Holding the animation is only half the fix. The Mac sends the mute
    sentinel when the owner disconnects, so the speaker sits at 0 % between
    sessions; on the next connect shairport starts the audio while the fade-up
    is still animating, and waiting for it to settle left ~2 s of silence over
    live content. So the session's first act is to restore the level the owner
    last listened at — before the hold, not after it — and the animation the
    hold swallows never reaches the speaker.
    """
    level = CONNECT_FADE_UP[-1]
    port = control_stub.server_port
    _run_hook(_db_for(level), runtime_dir=tmp_path, port=port)
    _run_hook("-144.000000", runtime_dir=tmp_path, port=port)
    assert [body["percent"] for _, body in _Recorder.posts] == [level, 0]
    opened = len(_Recorder.posts)

    _run_hook("--session-start", runtime_dir=tmp_path)
    started = time.monotonic()
    _fire_burst(
        CONNECT_FADE_UP,
        runtime_dir=tmp_path,
        port=port,
        spacing=FADE_SPACING_S,
    )
    finished = time.monotonic()

    session = _Recorder.posts[opened:]
    # One write: the restore. The settled level is the same value, so the
    # post that used to be the session's only one is skipped as delivered.
    assert [body["percent"] for _, body in session] == [level]
    # Still the session's opening write, so a mute latched at the speaker is
    # still left alone (ADR-0206).
    assert session[0][1].get("observation_initial") is True
    # Inside the animation's first few steps, not after the settle-only
    # hook's ~2 s course — a fraction of this burst's own wall time rather
    # than a fixed deadline, so a slow runner (stretching both alike) still
    # tells the two cases apart.
    assert _Recorder.times[opened] - started < (finished - started) / 2


@requires_flock
def test_the_same_ramp_without_a_session_start_still_tracks_the_slider(
    control_stub, tmp_path,
):
    """Unmuting at the sender replays the same ramp shape with no marker in
    front of it. That is a live volume change, not a connect snapshot, so it
    must keep following the slider from where the ramp starts."""
    _fire_burst(
        CONNECT_FADE_UP,
        runtime_dir=tmp_path,
        port=control_stub.server_port,
        spacing=FADE_SPACING_S,
    )

    percents = [body["percent"] for _, body in _Recorder.posts]
    assert len(percents) > 1
    assert percents[0] < percents[-1]
    assert percents[-1] == CONNECT_FADE_UP[-1]
    assert percents == sorted(percents)


# ---------- template wiring ------------------------------------------------


def test_template_points_shairport_at_the_installed_hook():
    """The renderer substitutes placeholders only; these values ship as-is, so
    the template and the install step must name the same path."""
    conf = SHAIRPORT_TEMPLATE.read_text(encoding="utf-8")
    installer = (
        REPO / "deploy" / "lib" / "install" / "renderers.sh"
    ).read_text(encoding="utf-8")

    assert (
        template_string_value(conf, "run_this_when_volume_is_set")
        == INSTALLED_HOOK_PATH
    )
    assert template_string_value(conf, "run_this_before_play_begins") == (
        f"{INSTALLED_HOOK_PATH} --session-start"
    )
    assert INSTALLED_HOOK_PATH in installer


def test_template_leaves_the_lane_at_unity_so_camilla_is_the_only_fader():
    """With shairport applying its own softvol the sender's gain compounds
    with the master and quantizes in the lane's 16 bits. Unity keeps one
    fader, in CamillaDSP's float master."""
    conf = SHAIRPORT_TEMPLATE.read_text(encoding="utf-8")

    assert template_string_value(conf, "ignore_volume_control") == "yes"
