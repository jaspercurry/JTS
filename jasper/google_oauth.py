# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Google hosted OAuth redirect. See :mod:`jasper.oauth_redirect`."""
from __future__ import annotations

from .oauth_redirect import resolved_redirect_uri

GOOGLE_OAUTH_CALLBACK_BASE = (
    "https://jaspercurry.github.io/google-oauth-callback/"
)


def resolved_google_redirect_uri() -> str:
    """``GOOGLE_REDIRECT_URI``, else the hosted callback for this speaker."""
    return resolved_redirect_uri(
        GOOGLE_OAUTH_CALLBACK_BASE, "GOOGLE_REDIRECT_URI",
    )
