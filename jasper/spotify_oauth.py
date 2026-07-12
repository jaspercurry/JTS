# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Import-cheap shared Spotify OAuth redirect defaults.

Callers retain ownership of environment overrides and hostname fallback policy;
this module owns only the byte-exact hosted callback base and its URI shape.
"""
from __future__ import annotations


SPOTIFY_OAUTH_CALLBACK_BASE = (
    "https://jaspercurry.github.io/spotify-oauth-callback/"
)


def default_spotify_redirect_uri(hostname: str) -> str:
    """Build the hosted Spotify OAuth redirect for ``hostname`` exactly."""
    return f"{SPOTIFY_OAUTH_CALLBACK_BASE}?host={hostname}"
