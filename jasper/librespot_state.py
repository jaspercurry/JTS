# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reader for the librespot state file written by the --onevent hook.

librespot (rust) exposes no local control surface, so
`deploy/bin/jasper-librespot-event` merges each player event's env vars
into a `KEY=value` file and this module is the read side — mux,
volume_observers and RendererClient all consult it for "is Spotify
active?" and "what's its current volume?".

Why a file rather than a socket: the hook is fire-and-forget, one process
per event, so a listener would have to exist somewhere; several consumers
read independently; and a restart mid-session picks the last known state
back up without re-syncing. The format is deliberately loose — extend it
by adding keys as librespot adds env vars; readers tolerate missing ones.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .env_file import parse_env_lines

logger = logging.getLogger(__name__)

DEFAULT_PATH = "/run/librespot/state.env"

# librespot reports volume as raw 0-65535 (16-bit) regardless of the
# `--volume-range` flag. The flag controls the dB curve mapping;
# the raw 0-65535 is the position. Convert with simple division.
LIBRESPOT_VOLUME_MAX = 65535

# Keys the hook writes as 1/0 and callers compare with `is True`.
_BOOL_KEYS = frozenset({"playing", "paused", "stopped", "session_active"})


def configured_path() -> str:
    """The state file this box uses: JASPER_LIBRESPOT_STATE or the default.

    The hook reads the same variable, so an override moves both ends.
    """
    return os.environ.get("JASPER_LIBRESPOT_STATE", DEFAULT_PATH)


def parse(text: str) -> dict[str, Any]:
    """Decode state-file text into the state dict (keys lowercased,
    `_BOOL_KEYS` as bools, every other value as the written string).

    An empty result means the text carried no assignments at all, which
    callers that distinguish "unknown" from "stopped" treat as unknown.

    Values round-trip verbatim apart from surrounding whitespace and
    anything past an embedded newline; librespot's event vars are ids,
    integers, URIs and enums, so neither occurs.
    """
    state: dict[str, Any] = {}
    for key, value in parse_env_lines(text):
        if value is None:
            continue
        name = key.lower()
        state[name] = value == "1" if name in _BOOL_KEYS else value
    return state


def read(path: str | None = None) -> dict[str, Any]:
    """Return the current state dict, or empty dict on read error.
    Safe to call any number of times; cheap (small file)."""
    p = Path(path or DEFAULT_PATH)
    try:
        return parse(p.read_text())
    except FileNotFoundError:
        # Absent until Spotify first plays; volume_observers polls this at
        # 1 Hz, so logging here is pure spam on every speaker that hasn't
        # used Spotify yet.
        return {}
    except OSError as e:
        logger.debug("librespot state read failed (%s): %s", p, e)
        return {}


def volume_percent(path: str | None = None) -> int | None:
    """Current Spotify volume as 0-100, or None if unknown.
    librespot reports raw 0-65535 (16-bit); we map to percent."""
    state = read(path)
    raw = state.get("volume")
    if raw is None:
        return None
    try:
        return max(0, min(100, round(int(raw) * 100 / LIBRESPOT_VOLUME_MAX)))
    except (TypeError, ValueError):
        return None


def track_uri(path: str | None = None) -> str | None:
    """Current track URI (e.g. spotify:track:6IiSsjuKiOIbOCSv10SqPn),
    or None if no track. Sufficient for "is something playing" and
    track-change detection. Resolving to title/artist requires a
    Spotify Web API call (see jasper.spotify_router)."""
    state = read(path)
    uri = state.get("uri") or state.get("track_id") or state.get("new_track_id")
    return uri or None
