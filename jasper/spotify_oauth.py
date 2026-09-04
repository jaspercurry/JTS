# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Spotify's hosted OAuth redirect: which page, which override.

Runtime consumers want ``resolved_spotify_redirect_uri``; the builder is
for callers that name a hostname other than this speaker's. The resolution
rule itself is :mod:`jasper.oauth_redirect`, shared with Google.
"""
from __future__ import annotations

from .oauth_redirect import hosted_redirect_uri, resolved_redirect_uri

SPOTIFY_OAUTH_CALLBACK_BASE = (
    "https://jaspercurry.github.io/spotify-oauth-callback/"
)


def default_spotify_redirect_uri(hostname: str) -> str:
    """Build the hosted Spotify OAuth redirect for ``hostname`` exactly."""
    return hosted_redirect_uri(SPOTIFY_OAUTH_CALLBACK_BASE, hostname)


def resolved_spotify_redirect_uri() -> str:
    """``SPOTIFY_REDIRECT_URI``, else the hosted callback for this speaker."""
    return resolved_redirect_uri(
        SPOTIFY_OAUTH_CALLBACK_BASE, "SPOTIFY_REDIRECT_URI",
    )
