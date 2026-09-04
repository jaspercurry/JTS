# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Google's hosted OAuth redirect: which page, which override.

Runtime consumers want ``resolved_google_redirect_uri``; the builder is
for callers that name a hostname other than this speaker's. The resolution
rule itself is :mod:`jasper.oauth_redirect`, shared with Spotify.
"""
from __future__ import annotations

from .oauth_redirect import hosted_redirect_uri, resolved_redirect_uri

GOOGLE_OAUTH_CALLBACK_BASE = (
    "https://jaspercurry.github.io/google-oauth-callback/"
)


def default_google_redirect_uri(hostname: str) -> str:
    """Build the hosted Google OAuth redirect for ``hostname`` exactly."""
    return hosted_redirect_uri(GOOGLE_OAUTH_CALLBACK_BASE, hostname)


def resolved_google_redirect_uri() -> str:
    """``GOOGLE_REDIRECT_URI``, else the hosted callback for this speaker."""
    return resolved_redirect_uri(
        GOOGLE_OAUTH_CALLBACK_BASE, "GOOGLE_REDIRECT_URI",
    )
