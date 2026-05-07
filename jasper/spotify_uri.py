"""Spotify playlist URL/URI normalisation.

Users paste links from Spotify desktop's Share → Copy link, which
yields shapes like:

    https://open.spotify.com/playlist/37i9dQZEVXcAAAAAAAAAA?si=abc
    https://open.spotify.com/intl-pt/playlist/37i9dQZEVXcAAAAAAAAAA
    spotify:playlist:37i9dQZEVXcAAAAAAAAAA
    37i9dQZEVXcAAAAAAAAAA

`parse_playlist_uri` reduces all of these to the canonical
`spotify:playlist:<id>` URI that the Web API accepts in
`start_playback(context_uri=...)`. Everything else returns None.

Spotify playlist IDs are 22-char base62 (`[A-Za-z0-9]{22}`). The
algorithmic ones (Discover Weekly, Daily Mix N, Release Radar) use
the same ID shape, just with reserved prefixes — `37i9dQZE…` for
personalized, `37i9dQZF…` for editorial. We don't need to
discriminate; the URI flows straight to start_playback either way.
"""
from __future__ import annotations

import re

# 22-char base62. Anchored with non-greedy boundaries; the calling
# regex below provides the framing.
_PLAYLIST_ID = r"[A-Za-z0-9]{22}"

# Three input shapes, tried in order.
_URI_RE = re.compile(rf"^spotify:playlist:({_PLAYLIST_ID})$")
_URL_RE = re.compile(
    rf"^https?://(?:open|play)\.spotify\.com/"
    rf"(?:intl-[a-z]{{2}}/)?"   # optional locale segment
    rf"playlist/({_PLAYLIST_ID})"
    rf"(?:[/?#].*)?$"
)
_BARE_ID_RE = re.compile(rf"^({_PLAYLIST_ID})$")


def parse_playlist_uri(text: str) -> str | None:
    """Normalise any reasonable Spotify-playlist input to
    `spotify:playlist:<id>`. Returns None if the input is not
    recognisable.

    Whitespace is trimmed; surrounding angle brackets (from email
    auto-linkification) are stripped.
    """
    if not text:
        return None
    s = text.strip().strip("<>").strip()
    if not s:
        return None
    for regex in (_URI_RE, _URL_RE, _BARE_ID_RE):
        m = regex.match(s)
        if m:
            return f"spotify:playlist:{m.group(1)}"
    return None


def playlist_id_from_uri(uri: str) -> str | None:
    """Inverse: pull the 22-char ID out of a normalised URI. Used by
    callers that need to hand the ID to spotipy methods that take it
    raw (e.g. `sp.playlist(playlist_id, ...)`)."""
    m = _URI_RE.match(uri or "")
    return m.group(1) if m else None
