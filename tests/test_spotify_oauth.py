# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the shared OAuth callback defaults."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jasper.google_oauth import GOOGLE_OAUTH_CALLBACK_BASE
from jasper.spotify_oauth import (
    SPOTIFY_OAUTH_CALLBACK_BASE,
    default_spotify_redirect_uri,
    resolved_spotify_redirect_uri,
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


@pytest.fixture(scope="module")
def string_constants_by_module() -> dict[str, list[str]]:
    """Every non-docstring string constant under jasper/, keyed by module.

    Parsed once: the owner pin below asks this same tree one question per
    callback base, and re-parsing all of jasper/ per parameter is the whole
    runtime of this file.
    """
    by_module: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "jasper").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_nodes(tree)
        by_module[path.relative_to(REPO_ROOT).as_posix()] = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and node not in docstrings
            and isinstance(node.value, str)
        ]
    return by_module


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


def test_existing_spotify_redirect_aliases_share_the_domain_owner() -> None:
    from jasper.control import volume_ops
    from jasper.web import spotify_setup

    assert volume_ops.SPOTIFY_OAUTH_CALLBACK_BASE == SPOTIFY_OAUTH_CALLBACK_BASE
    assert (
        spotify_setup.DEFAULT_BOUNCE_REDIRECT_URI_BASE
        == SPOTIFY_OAUTH_CALLBACK_BASE
    )


def test_resolved_spotify_redirect_uri_prefers_the_env_override(monkeypatch) -> None:
    monkeypatch.setenv("JASPER_HOSTNAME", "jts3.local")
    monkeypatch.delenv("SPOTIFY_REDIRECT_URI", raising=False)
    assert resolved_spotify_redirect_uri() == default_spotify_redirect_uri(
        "jts3.local"
    )
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "https://example.test/callback")
    assert resolved_spotify_redirect_uri() == "https://example.test/callback"


def test_resolved_spotify_redirect_uri_uses_the_recorded_hostname(
    monkeypatch, tmp_path,
) -> None:
    """Off-unit (no EnvironmentFile) the redirect must still name this box,
    or the code bounces to a different speaker."""
    identity_file = tmp_path / "identity.env"
    identity_file.write_text(
        "JASPER_IDENTITY_CONFIGURED_HOSTNAME=jts5.local\n", encoding="utf-8",
    )
    monkeypatch.delenv("JASPER_HOSTNAME", raising=False)
    monkeypatch.delenv("SPOTIFY_REDIRECT_URI", raising=False)
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(identity_file))
    assert resolved_spotify_redirect_uri() == default_spotify_redirect_uri(
        "jts5.local"
    )


@pytest.mark.parametrize(
    "base, owner",
    [
        (SPOTIFY_OAUTH_CALLBACK_BASE, "jasper/spotify_oauth.py"),
        (GOOGLE_OAUTH_CALLBACK_BASE, "jasper/google_oauth.py"),
    ],
)
def test_callback_base_literal_has_one_python_owner(
    base: str, owner: str, string_constants_by_module: dict[str, list[str]],
) -> None:
    owners = [
        module
        for module, strings in string_constants_by_module.items()
        if any(base in value for value in strings)
    ]
    assert owners == [owner]
