# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Spotify hosted OAuth redirect. See :mod:`jasper.oauth_redirect`."""
from __future__ import annotations

from .oauth_redirect import resolved_redirect_uri

SPOTIFY_OAUTH_CALLBACK_BASE = (
    "https://jaspercurry.github.io/spotify-oauth-callback/"
)


def resolved_spotify_redirect_uri() -> str:
    """``SPOTIFY_REDIRECT_URI``, else the hosted callback for this speaker."""
    return resolved_redirect_uri(
        SPOTIFY_OAUTH_CALLBACK_BASE, "SPOTIFY_REDIRECT_URI",
    )
