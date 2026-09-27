# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib
import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from jasper.cli import google_auth, spotify_auth
from jasper.google_creds import GOOGLE_TOKEN_URI


ROOT = Path(__file__).resolve().parents[1]


def _project_scripts() -> dict[str, str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["scripts"]


def test_project_console_scripts_import() -> None:
    """Every advertised console script must import and expose its callable."""

    for script_name, target in _project_scripts().items():
        module_name, separator, attribute_path = target.partition(":")
        assert separator, f"{script_name}: entry point must be module:attribute"

        module = importlib.import_module(module_name)
        value = module
        for part in attribute_path.split("."):
            assert hasattr(value, part), (
                f"{script_name}: {target} is missing attribute {part!r}"
            )
            value = getattr(value, part)


def test_spotify_auth_cli_smoke(monkeypatch, tmp_path, capsys):
    cfg = SimpleNamespace(
        spotify_enabled=True, spotify_client_id="fixture-client",
        spotify_redirect_uri="http://127.0.0.1:8888/callback",
        spotify_cache_path=str(tmp_path / "spotify-cache.json"),
    )
    load_env = Mock()
    monkeypatch.setattr(spotify_auth.env_load, "load_env_files", load_env)
    monkeypatch.setattr(spotify_auth.Config, "from_env", lambda: cfg)
    auth = Mock()
    auth.get_authorize_url.return_value = "https://accounts.spotify.test/authorize"
    auth.parse_response_code.return_value = "fixture-code"
    auth_factory = Mock(return_value=auth)
    monkeypatch.setattr(spotify_auth, "SpotifyPKCE", auth_factory)
    redirect = cfg.spotify_redirect_uri + "?code=fixture-code"
    monkeypatch.setattr("builtins.input", lambda _: redirect)

    assert spotify_auth.main() is None

    load_env.assert_called_once_with()
    auth_factory.assert_called_once_with(
        client_id=cfg.spotify_client_id, redirect_uri=cfg.spotify_redirect_uri,
        scope=spotify_auth.SPOTIFY_SCOPE, cache_path=cfg.spotify_cache_path,
        open_browser=False,
    )
    auth.parse_response_code.assert_called_once_with(redirect)
    auth.get_access_token.assert_called_once_with("fixture-code")
    assert "fixture-code" not in capsys.readouterr().out


def test_google_auth_cli_smoke(monkeypatch, tmp_path, capsys):
    cfg = SimpleNamespace(
        google_enabled=True, google_client_id="fixture-client",
        google_client_secret="fixture-secret",
        google_redirect_uri="http://127.0.0.1:8888/callback",
        google_accounts_path=str(tmp_path / "accounts.json"),
    )
    load_env = Mock()
    monkeypatch.setattr(google_auth.env_load, "load_env_files", load_env)
    monkeypatch.setattr(google_auth.Config, "from_env", lambda: cfg)
    flow = Mock()
    flow.authorization_url.return_value = ("https://accounts.google.test/authorize", "member")
    flow.credentials = SimpleNamespace(
        refresh_token="fixture-refresh", scopes=["fixture-scope"], token_uri="",
    )
    factory = Mock(return_value=flow)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", SimpleNamespace(
        Flow=SimpleNamespace(from_client_config=factory),
    ))
    token_path = tmp_path / "member.json"
    monkeypatch.setattr(google_auth, "default_token_path_for", lambda _: str(token_path))
    monkeypatch.setattr("builtins.input", lambda _: cfg.google_redirect_uri + "?code=fixture-code")

    assert google_auth.main(["member", "--make-default"]) is None

    load_env.assert_called_once_with()
    factory.assert_called_once()
    assert factory.call_args.kwargs == {"scopes": google_auth.GOOGLE_SCOPES, "state": "member"}
    assert factory.call_args.args[0]["web"]["client_id"] == cfg.google_client_id
    assert flow.redirect_uri == cfg.google_redirect_uri
    flow.authorization_url.assert_called_once_with(
        access_type="offline", prompt="consent", include_granted_scopes="true",
    )
    flow.fetch_token.assert_called_once_with(code="fixture-code")
    registry = google_auth.GoogleRegistry.load(cfg.google_accounts_path)
    assert registry.default_name == "member"
    assert registry.get("member").token_path == str(token_path)
    saved = json.loads(token_path.read_text())
    assert saved == {
        "refresh_token": "fixture-refresh", "scopes": ["fixture-scope"],
        "token_uri": GOOGLE_TOKEN_URI,
    }
    output = capsys.readouterr().out
    assert not any(value in output for value in ("fixture-secret", "fixture-refresh", "fixture-code"))
