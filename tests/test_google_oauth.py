# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the shared Google OAuth callback default.

The single-owner pin for ``GOOGLE_OAUTH_CALLBACK_BASE`` lives beside its
Spotify twin in tests/test_spotify_oauth.py, parametrized over both bases.
"""
from __future__ import annotations

from jasper.google_oauth import (
    GOOGLE_OAUTH_CALLBACK_BASE,
    default_google_redirect_uri,
    resolved_google_redirect_uri,
)


def test_default_google_redirect_uri_preserves_exact_hostname() -> None:
    assert GOOGLE_OAUTH_CALLBACK_BASE == (
        "https://jaspercurry.github.io/google-oauth-callback/"
    )
    assert default_google_redirect_uri("kitchen.local") == (
        "https://jaspercurry.github.io/google-oauth-callback/?host=kitchen.local"
    )
    # Hostname fallback/validation belongs to each caller. The shared builder
    # must not silently normalize a blank or otherwise caller-supplied value.
    assert default_google_redirect_uri("").endswith("?host=")


def test_resolved_google_redirect_uri_prefers_the_env_override(monkeypatch) -> None:
    monkeypatch.setenv("JASPER_HOSTNAME", "jts3.local")
    monkeypatch.delenv("GOOGLE_REDIRECT_URI", raising=False)
    assert resolved_google_redirect_uri() == default_google_redirect_uri(
        "jts3.local"
    )
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://example.test/callback")
    assert resolved_google_redirect_uri() == "https://example.test/callback"


def test_resolved_google_redirect_uri_uses_the_recorded_hostname(
    monkeypatch, tmp_path,
) -> None:
    """Off-unit (no EnvironmentFile) the redirect must still name this box,
    or Google bounces the browser to a different speaker."""
    identity_file = tmp_path / "identity.env"
    identity_file.write_text(
        "JASPER_IDENTITY_CONFIGURED_HOSTNAME=jts5.local\n", encoding="utf-8",
    )
    monkeypatch.delenv("JASPER_HOSTNAME", raising=False)
    monkeypatch.delenv("GOOGLE_REDIRECT_URI", raising=False)
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(identity_file))
    assert resolved_google_redirect_uri() == default_google_redirect_uri(
        "jts5.local"
    )
