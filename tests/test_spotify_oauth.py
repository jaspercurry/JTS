# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the shared Spotify OAuth callback default."""
from __future__ import annotations

import ast
from pathlib import Path

from jasper.spotify_oauth import (
    SPOTIFY_OAUTH_CALLBACK_BASE,
    default_spotify_redirect_uri,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _docstring_nodes(tree: ast.AST) -> set[ast.Constant]:
    nodes: set[ast.Constant] = set()
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            nodes.add(first.value)
    return nodes


def test_default_spotify_redirect_uri_preserves_exact_hostname() -> None:
    assert SPOTIFY_OAUTH_CALLBACK_BASE == (
        "https://jaspercurry.github.io/spotify-oauth-callback/"
    )
    assert default_spotify_redirect_uri("kitchen.local") == (
        "https://jaspercurry.github.io/spotify-oauth-callback/?host=kitchen.local"
    )
    # Hostname fallback/validation belongs to each caller. The shared builder
    # must not silently normalize a blank or otherwise caller-supplied value.
    assert default_spotify_redirect_uri("").endswith("?host=")


def test_existing_spotify_redirect_aliases_share_the_domain_owner(
    monkeypatch,
) -> None:
    from jasper.control import volume_ops
    from jasper.web import spotify_setup

    assert volume_ops.SPOTIFY_OAUTH_CALLBACK_BASE == SPOTIFY_OAUTH_CALLBACK_BASE
    assert (
        spotify_setup.DEFAULT_BOUNCE_REDIRECT_URI_BASE
        == SPOTIFY_OAUTH_CALLBACK_BASE
    )
    assert spotify_setup._default_bounce_redirect_uri("jts3.local") == (
        default_spotify_redirect_uri("jts3.local")
    )

    monkeypatch.setenv("JASPER_HOSTNAME", "jts3.local")
    monkeypatch.delenv("SPOTIFY_REDIRECT_URI", raising=False)
    assert volume_ops._spotify_redirect_uri() == default_spotify_redirect_uri(
        "jts3.local"
    )
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "https://example.test/callback")
    assert volume_ops._spotify_redirect_uri() == "https://example.test/callback"


def test_callback_base_literal_has_one_python_owner() -> None:
    owners: list[str] = []
    for path in sorted((REPO_ROOT / "jasper").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_nodes(tree)
        if any(
            isinstance(node, ast.Constant)
            and node not in docstrings
            and isinstance(node.value, str)
            and SPOTIFY_OAUTH_CALLBACK_BASE in node.value
            for node in ast.walk(tree)
        ):
            owners.append(path.relative_to(REPO_ROOT).as_posix())

    assert owners == ["jasper/spotify_oauth.py"]
