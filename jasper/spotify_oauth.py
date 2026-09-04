# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared Spotify OAuth redirect: its shape and its resolution.

Runtime consumers want ``resolved_spotify_redirect_uri``; the builder is
for callers that name a hostname other than this speaker's.
"""
from __future__ import annotations

import os

from .identity import resolve_hostname

SPOTIFY_OAUTH_CALLBACK_BASE = (
    "https://jaspercurry.github.io/spotify-oauth-callback/"
)


def default_spotify_redirect_uri(hostname: str) -> str:
    """Build the hosted Spotify OAuth redirect for ``hostname`` exactly."""
    return f"{SPOTIFY_OAUTH_CALLBACK_BASE}?host={hostname}"


def resolved_spotify_redirect_uri() -> str:
    """``SPOTIFY_REDIRECT_URI``, else the hosted callback for this speaker.

    Spotify matches it against the Developer App's registered URIs
    byte-for-byte, so every consumer must resolve it identically.
    """
    override = os.environ.get("SPOTIFY_REDIRECT_URI", "").strip()
    return override or default_spotify_redirect_uri(resolve_hostname())
