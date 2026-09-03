# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end pin for the librespot --onevent hook.

The hook is POSIX sh, so the only honest test runs it for real and reads
the result back through its consumers, ``jasper.librespot_state`` and
``jasper.source_state``.
"""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from jasper import librespot_state, source_state

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "bin" / "jasper-librespot-event"
)

# One Connect session, one event per step: the env librespot exports, and
# the state keys that event is expected to CHANGE. Everything else must
# survive untouched, so the test overlays each delta onto the previous
# expectation and demands an exact match — that overlay is the merge
# contract consumers rely on when they read between events.
# last_event/player_event track PLAYER_EVENT on every step and are added
# automatically below.
SESSION = (
    ({"PLAYER_EVENT": "session_connected", "CLIENT_ID": "cid"},
     {"client_id": "cid", "session_active": True}),
    ({"PLAYER_EVENT": "playing", "TRACK_ID": "spotify:track:X", "VOLUME": "32768"},
     {"track_id": "spotify:track:X", "volume": "32768",
      "playing": True, "paused": False, "stopped": False}),
    ({"PLAYER_EVENT": "volume_changed", "VOLUME": "65535"},
     {"volume": "65535"}),
    ({"PLAYER_EVENT": "paused"},
     {"playing": False, "paused": True, "stopped": False}),
    ({"PLAYER_EVENT": "session_disconnected"},
     {"playing": False, "paused": False, "stopped": True,
      "session_active": False}),
)


def _fire(state: Path, env: dict[str, str]) -> None:
    subprocess.run(
        ["sh", str(SCRIPT)],
        env={"PATH": "/usr/bin:/bin", "JASPER_LIBRESPOT_STATE": str(state), **env},
        check=True,
    )


async def test_session_events_merge_into_reader_state(tmp_path):
    """Every event leaves a complete, world-readable state file.

    0644 matters on both create and replace: librespot runs as pi:audio
    while jasper-mux/control read the file as jasper:*, so a tighter mode
    fails Spotify source detection closed.
    """
    state = tmp_path / "run" / "librespot" / "state.env"
    expected: dict[str, object] = {}

    for env, delta in SESSION:
        _fire(state, env)
        expected |= {
            "last_event": env["PLAYER_EVENT"],
            "player_event": env["PLAYER_EVENT"],
        } | delta
        assert librespot_state.read(str(state)) == expected
        assert await source_state.spotify_playing(str(state)) is (
            expected.get("playing") is True
        )
        assert stat.S_IMODE(state.stat().st_mode) == 0o644
        assert list(state.parent.iterdir()) == [state]

    assert librespot_state.volume_percent(str(state)) == 100
    assert librespot_state.track_uri(str(state)) == "spotify:track:X"


@pytest.mark.parametrize("value", [
    'a b  c',
    'He said "stop" \\ then left',
    "key=value=more",
    "trailing hash # here",
    "#leading-hash",
    "$(touch pwned) `id` ${HOME} *",
    "Björk — Jóga ✓",
])
def test_opaque_values_survive_the_round_trip(tmp_path, value):
    """Track URIs and reasons are opaque text the hook must not mangle or
    expand: quotes, backslashes, `=`, `#` and non-ASCII come back verbatim,
    both as written and after a later event carries them forward."""
    state = tmp_path / "state.env"

    _fire(state, {"PLAYER_EVENT": "track_changed", "URI": value})
    assert librespot_state.read(str(state))["uri"] == value

    _fire(state, {"PLAYER_EVENT": "playing"})
    assert librespot_state.read(str(state))["uri"] == value
    assert list(state.parent.iterdir()) == [state]
