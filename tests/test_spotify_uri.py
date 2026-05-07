from __future__ import annotations

import pytest

from jasper.spotify_uri import parse_playlist_uri, playlist_id_from_uri


_DW_ID = "37i9dQZEVXcAAAAAAAAAAA"   # 22-char base62, Discover-Weekly-shape
_USER_ID = "5fGr9XJDxqZAYJlhxAvT8w"  # 22-char base62, regular user playlist


@pytest.mark.parametrize("text,expected", [
    # Canonical URI passes through.
    (f"spotify:playlist:{_DW_ID}", f"spotify:playlist:{_DW_ID}"),
    # Open Spotify share URL (https), with the ?si= tracking param.
    (f"https://open.spotify.com/playlist/{_DW_ID}?si=abcd1234", f"spotify:playlist:{_DW_ID}"),
    # Locale-prefixed URL (Spotify localises share links per market).
    (f"https://open.spotify.com/intl-pt/playlist/{_DW_ID}", f"spotify:playlist:{_DW_ID}"),
    # http (rare but seen).
    (f"http://open.spotify.com/playlist/{_USER_ID}", f"spotify:playlist:{_USER_ID}"),
    # Trailing slash, no query.
    (f"https://open.spotify.com/playlist/{_USER_ID}/", f"spotify:playlist:{_USER_ID}"),
    # Bare 22-char ID — accepted (some users only paste the ID).
    (_USER_ID, f"spotify:playlist:{_USER_ID}"),
    # Whitespace and angle brackets stripped (email/Slack auto-link shape).
    (f"  <https://open.spotify.com/playlist/{_DW_ID}>  ", f"spotify:playlist:{_DW_ID}"),
])
def test_parse_playlist_uri_accepts_common_shapes(text: str, expected: str) -> None:
    assert parse_playlist_uri(text) == expected


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "not a url",
    "https://open.spotify.com/track/abc123",        # different entity
    "https://open.spotify.com/playlist/short",      # too few chars
    "spotify:playlist:short",
    "https://example.com/playlist/" + _DW_ID,        # wrong host
    "37i9dQZE",                                       # bare but too short
    "spotify:track:" + _DW_ID,                        # wrong type
])
def test_parse_playlist_uri_rejects_invalid(text: str) -> None:
    assert parse_playlist_uri(text) is None


def test_playlist_id_round_trip() -> None:
    uri = parse_playlist_uri(f"https://open.spotify.com/playlist/{_DW_ID}?si=x")
    assert playlist_id_from_uri(uri) == _DW_ID


def test_playlist_id_from_uri_rejects_garbage() -> None:
    assert playlist_id_from_uri("") is None
    assert playlist_id_from_uri("not a uri") is None
    assert playlist_id_from_uri("spotify:track:" + _USER_ID) is None
