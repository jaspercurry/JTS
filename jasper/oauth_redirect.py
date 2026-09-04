# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One shape for every hosted OAuth bounce redirect.

Neither Spotify nor Google accepts an mDNS ``.local`` name or a bare LAN IP
as a redirect host — only a public TLD or ``localhost`` — which is all a Pi
on a household network has. So each provider registers a static GitHub Pages
page whose ``?host=`` names the speaker to bounce back to. Both then match
the registered URI byte-for-byte, so the override-and-fallback rule lives
here once; each provider's leaf owns only its callback base and env var.
"""
from __future__ import annotations

import os

from .identity import resolve_hostname


def hosted_redirect_uri(base: str, hostname: str) -> str:
    """The hosted callback for ``hostname`` exactly — never normalized."""
    return f"{base}?host={hostname}"


def resolved_redirect_uri(base: str, env_var: str) -> str:
    """``env_var``'s value, else the hosted callback for this speaker."""
    override = os.environ.get(env_var, "").strip()
    return override or hosted_redirect_uri(base, resolve_hostname())
