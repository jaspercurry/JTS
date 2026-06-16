"""Unit tests for jasper.google_creds.

Covers the registry CRUD + token-file persistence shape. The actual
google-auth refresh path can't be exercised without a real OAuth
session, so it's mocked via monkeypatch — we verify the loader's
'no token file → None' silent-disable contract and the OK path with
a stand-in Credentials object.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from jasper import google_creds as gc
from jasper.google_creds import (
    GoogleAccount,
    GoogleClients,
    GoogleRegistry,
    default_token_path_for,
    load_credentials,
    save_token,
    valid_access_token,
)


# --- helpers ------------------------------------------------------


def _tmp_path(suffix: str = ".json") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    os.unlink(path)
    return path


# --- registry round-trip ------------------------------------------


def test_registry_load_missing_returns_empty():
    r = GoogleRegistry.load("/nonexistent/google_accounts.json")
    assert r.accounts == []
    assert r.default_name == ""


def test_registry_round_trip(tmp_path):
    path = str(tmp_path / "accounts.json")
    r = GoogleRegistry(path=path)
    r.add_or_update(
        GoogleAccount(name="jasper", email="jasper@gmail.com",
                      display_name="Jasper Curry"),
        make_default=True,
    )
    r.add_or_update(GoogleAccount(name="brittany"))
    r.save()

    r2 = GoogleRegistry.load(path)
    assert len(r2.accounts) == 2
    assert r2.default_name == "jasper"
    assert r2.get("jasper").email == "jasper@gmail.com"
    assert r2.get("jasper").display_name == "Jasper Curry"
    # default_token_path_for fills in when the caller didn't specify
    assert r2.get("brittany").token_path.endswith("brittany.json")


def test_registry_default_falls_back_to_first_account_when_unset():
    r = GoogleRegistry()
    r.add_or_update(GoogleAccount(name="jasper"))
    r.add_or_update(GoogleAccount(name="brittany"))
    # First add_or_update sets default; remove default and verify
    # default() falls through to the first remaining account.
    r.default_name = ""
    assert r.default() is not None
    assert r.default().name == "jasper"


def test_registry_remove_updates_default():
    r = GoogleRegistry()
    r.add_or_update(GoogleAccount(name="jasper"), make_default=True)
    r.add_or_update(GoogleAccount(name="brittany"))
    assert r.default_name == "jasper"
    r.remove("jasper")
    assert r.default_name == "brittany"
    r.remove("brittany")
    assert r.default_name == ""


def test_default_token_path_blocks_traversal():
    p = default_token_path_for("alice/../etc/passwd")
    assert "/etc/passwd" not in p
    assert p.startswith("/var/lib/jasper/google/tokens/")
    assert p.endswith(".json")


def test_add_or_update_preserves_existing_email_when_arg_blank():
    """Re-running OAuth (e.g. user revokes + re-links) shouldn't blow
    away a previously-fetched email/display_name. The wizard's start-
    OAuth path constructs a freshly-empty Account before the callback;
    add_or_update must not overwrite filled-in metadata with blanks."""
    r = GoogleRegistry()
    r.add_or_update(
        GoogleAccount(name="jasper", email="jasper@gmail.com",
                      display_name="Jasper Curry"),
        make_default=True,
    )
    r.add_or_update(GoogleAccount(name="jasper", token_path="/new/path.json"))
    a = r.get("jasper")
    assert a.token_path == "/new/path.json"
    assert a.email == "jasper@gmail.com"
    assert a.display_name == "Jasper Curry"


# --- token I/O ----------------------------------------------------


def test_save_token_refuses_empty_refresh_token(tmp_path):
    path = str(tmp_path / "tok.json")
    with pytest.raises(ValueError, match="refresh_token"):
        save_token(path, refresh_token="")


def test_save_token_writes_mode_0640(tmp_path):
    path = str(tmp_path / "tok.json")
    save_token(
        path,
        refresh_token="1//0gFAKE",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    st = os.stat(path)
    # 0o640 (WS1 Phase 3b): group-`jasper` read so the now-non-root jasper-voice
    # can read its OAuth refresh token after systemd's StateDirectory
    # recursive-chown re-owns the file to another jasper daemon. NO world read
    # (the token is still a secret). Per-daemon isolation is Phase 4.
    assert stat.S_IMODE(st.st_mode) == 0o640
    payload = json.loads(Path(path).read_text())
    # The CLIENT_ID/SECRET fields are intentionally NOT persisted —
    # they live in the env file and are recombined at load time.
    assert "client_id" not in payload
    assert "client_secret" not in payload
    assert payload["refresh_token"] == "1//0gFAKE"
    assert payload["token_uri"].endswith("/token")
    assert payload["scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]


_PYTHON_RUNTIME_SH = (
    Path(__file__).resolve().parents[1] / "deploy/lib/install/python-runtime.sh"
)


def test_install_creates_google_dir_setgid():
    """The Google tree's group-`jasper` access (so the non-root voice can read
    its OAuth tokens after the drop) is set authoritatively by install.sh as
    root, and setgid so tokens the root /google/ wizard writes inherit group
    `jasper` directly. Guard the setgid + group so the bit can't be silently
    dropped back to 0750 — which would re-break a freshly linked account."""
    sh = _PYTHON_RUNTIME_SH.read_text()
    assert "install -d -m 2750 -g jasper" in sh and "google" in sh, (
        "python-runtime.sh must create /var/lib/jasper/google setgid + group "
        "jasper (install -d -m 2750 -g jasper ...)"
    )


def test_save_token_atomic_replace_on_existing_file(tmp_path):
    path = str(tmp_path / "tok.json")
    save_token(path, refresh_token="first")
    save_token(path, refresh_token="second")
    payload = json.loads(Path(path).read_text())
    assert payload["refresh_token"] == "second"
    # No leftover .tmp
    assert not Path(path + ".tmp").exists()


# --- credential loading -------------------------------------------


def test_load_credentials_missing_token_path_returns_none():
    a = GoogleAccount(name="jasper", token_path="")
    assert load_credentials(a, client_id="x", client_secret="y") is None


def test_load_credentials_missing_file_returns_none(tmp_path):
    a = GoogleAccount(
        name="jasper", token_path=str(tmp_path / "missing.json"),
    )
    assert load_credentials(a, client_id="x", client_secret="y") is None


def test_load_credentials_malformed_json_returns_none(tmp_path):
    path = tmp_path / "tok.json"
    path.write_text("{not valid json")
    a = GoogleAccount(name="jasper", token_path=str(path))
    assert load_credentials(a, client_id="x", client_secret="y") is None


def test_load_credentials_missing_refresh_token_returns_none(tmp_path):
    """Token file shape is right but refresh_token field is missing —
    silent disable rather than crash. Matches the 'no token = no
    tools' contract."""
    path = tmp_path / "tok.json"
    path.write_text(json.dumps({
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": [],
    }))
    a = GoogleAccount(name="jasper", token_path=str(path))
    assert load_credentials(a, client_id="x", client_secret="y") is None


def test_load_credentials_returns_credentials_when_valid(tmp_path, monkeypatch):
    """OK path: token file present + parseable + has refresh_token →
    returns a Credentials object. We monkeypatch the refresh() so the
    test doesn't hit Google's network endpoint, but the construction +
    info-merge path runs for real."""
    path = tmp_path / "tok.json"
    save_token(str(path), refresh_token="1//0gREAL")
    a = GoogleAccount(name="jasper", token_path=str(path))

    # Make Credentials.refresh a no-op AND mark .valid True so
    # load_credentials returns without trying to hit Google.
    from google.oauth2.credentials import Credentials
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: None)
    monkeypatch.setattr(Credentials, "valid", True)

    creds = load_credentials(a, client_id="cid", client_secret="csec")
    assert creds is not None
    assert creds.refresh_token == "1//0gREAL"
    assert creds.client_id == "cid"
    assert creds.client_secret == "csec"


def test_valid_access_token_returns_none_when_no_token():
    a = GoogleAccount(name="jasper", token_path="/nonexistent.json")
    assert valid_access_token(a, client_id="x", client_secret="y") is None


# --- GoogleClients accessor ---------------------------------------


def test_resolve_account_default_when_arg_empty():
    r = GoogleRegistry()
    r.add_or_update(GoogleAccount(name="jasper"), make_default=True)
    r.add_or_update(GoogleAccount(name="brittany"))
    clients = GoogleClients(registry=r, client_id="x", client_secret="y")
    assert clients.resolve_account("") == "jasper"


def test_resolve_account_unknown_returns_none():
    r = GoogleRegistry()
    r.add_or_update(GoogleAccount(name="jasper"), make_default=True)
    clients = GoogleClients(registry=r, client_id="x", client_secret="y")
    assert clients.resolve_account("frank") is None


def test_resolve_account_named_match():
    r = GoogleRegistry()
    r.add_or_update(GoogleAccount(name="jasper"), make_default=True)
    r.add_or_update(GoogleAccount(name="brittany"))
    clients = GoogleClients(registry=r, client_id="x", client_secret="y")
    assert clients.resolve_account("brittany") == "brittany"


def test_list_account_names_in_insertion_order():
    r = GoogleRegistry()
    r.add_or_update(GoogleAccount(name="jasper"), make_default=True)
    r.add_or_update(GoogleAccount(name="brittany"))
    clients = GoogleClients(registry=r, client_id="x", client_secret="y")
    assert clients.list_account_names() == ["jasper", "brittany"]


def test_default_account_name_empty_registry_returns_none():
    clients = GoogleClients(
        registry=GoogleRegistry(), client_id="x", client_secret="y",
    )
    assert clients.default_account_name() is None


def test_build_calendar_passes_creds_to_factory(monkeypatch):
    """The injected service_factory receives the creds returned from
    credentials(); tests use this to skip the real google-auth refresh
    while still verifying the wiring."""
    r = GoogleRegistry()
    r.add_or_update(GoogleAccount(name="jasper"), make_default=True)
    sentinel_creds = object()
    seen = {}

    def fake_factory(api_name, version, creds):
        seen["call"] = (api_name, version, creds)
        return ("fake", api_name, version)

    monkeypatch.setattr(gc, "load_credentials", lambda *a, **kw: sentinel_creds)
    clients = GoogleClients(
        registry=r, client_id="x", client_secret="y",
        service_factory=fake_factory,
    )
    out = clients.build_calendar("jasper")
    assert out == ("fake", "calendar", "v3")
    assert seen["call"] == ("calendar", "v3", sentinel_creds)


def test_build_calendar_returns_none_when_credentials_fail(monkeypatch):
    r = GoogleRegistry()
    r.add_or_update(GoogleAccount(name="jasper"), make_default=True)
    monkeypatch.setattr(gc, "load_credentials", lambda *a, **kw: None)
    clients = GoogleClients(
        registry=r, client_id="x", client_secret="y",
        service_factory=lambda *a: pytest.fail("should not be called"),
    )
    assert clients.build_calendar("jasper") is None


def test_build_gmail_returns_none_for_unknown_account(monkeypatch):
    r = GoogleRegistry()
    r.add_or_update(GoogleAccount(name="jasper"), make_default=True)
    clients = GoogleClients(
        registry=r, client_id="x", client_secret="y",
        service_factory=lambda *a: pytest.fail("should not be called"),
    )
    # No load_credentials patch needed — registry.get returns None
    # before we get to credentials().
    assert clients.build_gmail("frank") is None
