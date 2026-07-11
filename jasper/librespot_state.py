# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reader for the librespot state file written by the --onevent hook.

librespot (rust) doesn't expose an HTTP control surface like
go-librespot did. Instead, we configure it with `--onevent` pointing
at a small script (`jasper-librespot-event`) that captures env vars
on every player event and atomically writes them to a JSON file
under `/run/librespot/state.json`. This module is the read side —
mux, volume_observers, and RendererClient all consult it for
"is Spotify active?" and "what's its current volume?".

Why a state file instead of, say, a Unix socket or a long-lived
event subscription:

- librespot's hook is fire-and-forget — one process per event.
  A socket or pipe would need a long-running listener; a state file
  needs nothing.
- Multiple consumers (mux, observer, renderer) read independently.
  The file is shared state with atomic semantics (POSIX rename).
- Crash safety: if jasper-voice restarts mid-session, the next
  read picks up the last known state without re-syncing.

The format is intentionally loose — extend by adding new fields
as librespot adds new env vars; readers tolerate missing keys.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PATH = "/run/librespot/state.json"

# librespot reports volume as raw 0-65535 (16-bit) regardless of the
# `--volume-range` flag. The flag controls the dB curve mapping;
# the raw 0-65535 is the position. Convert with simple division.
LIBRESPOT_VOLUME_MAX = 65535


def read(path: str | None = None) -> dict[str, Any]:
    """Return the current state dict, or empty dict on read error.
    Safe to call any number of times; cheap (small JSON file)."""
    p = Path(path or DEFAULT_PATH)
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.debug("librespot state read failed (%s): %s", p, e)
        return {}


def is_playing(path: str | None = None) -> bool:
    """True iff librespot is actively producing audio (track playing,
    not paused, not stopped). Wrapped by `source_state.spotify_playing`,
    which is the entry point both RendererClient and the mux daemon use."""
    state = read(path)
    if not state:
        return False
    # `playing` flag is set by the hook on PLAYER_EVENT=playing /
    # cleared on paused/stopped/session_disconnected. Belt-and-
    # suspenders: also check `paused` and `stopped` aren't True.
    if state.get("playing") is True:
        return True
    if state.get("paused") is True or state.get("stopped") is True:
        return False
    return False


def session_active(path: str | None = None) -> bool:
    """True if a Spotify Connect session is open (regardless of
    play/pause state).

    Currently unused: the renderer's "is Spotify the target source"
    decisions go through `track_uri()` and `source_state.spotify_playing()`
    instead, and `jasper.control.state_aggregate` re-implements this same
    `session_active` field lookup inline rather than calling this
    function."""
    state = read(path)
    return bool(state.get("session_active"))


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
