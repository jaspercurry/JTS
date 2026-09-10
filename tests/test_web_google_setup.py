# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free tests for the migrated /assistant/google/ wizard.

Covers two things:

1. Canonical render — each of the three state renderers
   (`_setup_wizard_html`, `_redirect_uri_page_html`, `_management_html`)
   emits the canonical document shell: the `/assets/app.css` link, the
   `.app-header` top bar, the `<meta name="jts-csrf">` tag, the hidden
   `csrf_token` form field, the page's ES module, and the page CSS link.
   These also assert the migration removed the legacy chrome (no old
   shell/style markers, no inline `<script>` in the body, no
   `jtsConfirmSubmit`/`window.confirm`).

2. Routing + behaviour preserved — the handler returned by `_make_handler`
   is driven through a fake request, with the `_common` plumbing patched so
   the test stays hardware-free. Asserts: GET / renders the right state,
   GET /callback exchanges the code and restarts voice, unknown POSTs 404,
   CSRF failure rejects, /setup-credentials validates + persists, /start
   begins OAuth, /remove + /default mutate the registry, and an unknown GET
   404s.

The public surface (`_index_html` analogue render fns, `make_server`,
`main`) is asserted importable so `jasper/web/__main__.py` keeps working.
"""
from __future__ import annotations

import importlib
import http
import logging
import urllib.parse
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread
from types import SimpleNamespace
from unittest import mock

import pytest

web_common = importlib.import_module("jasper.web._common")
google_setup = importlib.import_module("jasper.web.google_setup")


CSRF = "x" * 43
GOOD_CLIENT_ID = "123456789012-abcdefg.apps.googleusercontent.com"
REDIRECT = "https://jaspercurry.github.io/google-oauth-callback/?host=jts.local"


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Canonical render — shared assertions
# ---------------------------------------------------------------------------


def _assert_canonical(page: bytes) -> str:
    text = page.decode()
    # Canonical document shell.
    assert "/assets/app.css" in text, "app.css link missing"
    assert 'class="app-header"' in text, ".app-header top bar missing"
    assert 'name="jts-csrf"' in text, "CSRF meta tag missing"
    assert "/assets/google/google.css" in text, "page CSS link missing"
    assert '/assets/google/js/main.js' in text, "page ES module missing"
    # Legacy chrome must be gone.
    assert "PAGE" "_STYLE" not in text
    assert "jtsConfirmSubmit" not in text, "legacy inline confirm shim leaked"
    assert "window.confirm" not in text
    # No inline <script> blocks should remain in the body — all JS moved to
    # the ES module (the only <script> is the type=module src= loader).
    assert "<script>" not in text, "inline <script> leaked into body"
    return text


def test_setup_wizard_html_is_canonical():
    page = google_setup._setup_wizard_html(REDIRECT, CSRF)
    text = _assert_canonical(page)
    # State 1: the paste-creds form posts to setup-credentials, with the
    # hidden CSRF field and the four wizard steps.
    assert 'action="setup-credentials"' in text
    assert 'name="csrf_token"' in text
    assert 'class="setup-steps"' in text
    assert "data-step=\"4\"" in text
    assert "Create one Google Cloud OAuth client" in text
    assert "Connect this speaker to Google Calendar + Gmail" not in text


def test_redirect_uri_page_html_is_canonical():
    page = google_setup._redirect_uri_page_html(REDIRECT, GOOD_CLIENT_ID, CSRF)
    text = _assert_canonical(page)
    # State 2: add-account form + reset-credentials confirm guard.
    assert 'action="start"' in text
    assert 'action="reset-credentials"' in text
    assert "data-confirm" in text, "destructive confirm hook missing"
    # confirm-forms.js reads dataset.confirmDanger === "1" — a bare
    # data-confirm-danger attribute silently loses the red/danger style.
    assert 'data-confirm-danger="1"' in text
    assert 'name="csrf_token"' in text


def test_management_html_is_canonical_and_escapes_accounts():
    accounts = [
        google_setup.GoogleAccount(
            name="jasper", token_path="/x", email="jasper@example.com",
        ),
        google_setup.GoogleAccount(
            name="britt", token_path="/y", email="britt@example.com",
        ),
    ]
    registry = SimpleNamespace(accounts=accounts, default_name="jasper")
    page = google_setup._management_html(registry, REDIRECT, GOOD_CLIENT_ID, CSRF)
    text = _assert_canonical(page)
    # State 3: linked accounts list + per-account default/remove forms.
    assert 'class="accounts"' in text
    assert "jasper@example.com" in text
    assert 'action="remove"' in text
    assert 'action="default"' in text
    # confirm-forms.js reads dataset.confirmDanger === "1" — a bare
    # data-confirm-danger attribute silently loses the red/danger style.
    assert 'data-confirm-danger="1"' in text
    # Default badge present with the OK status tone.
    assert "badge badge--ok" in text


def test_management_page_keeps_google_copy_short_on_default_path():
    account = google_setup.GoogleAccount(
        name="jasper", token_path="/x", email="jasper@example.com",
    )
    registry = SimpleNamespace(accounts=[account], default_name="jasper")
    text = google_setup._management_html(registry, REDIRECT, GOOD_CLIENT_ID, CSRF).decode()

    assert "Linked Google accounts let JTS answer Calendar and Gmail questions" in text
    assert "Add another household member below" in text
    assert "Google Cloud setup guide" in text
    assert "voice loop reads Calendar + Gmail data per-account" not in text
    assert "Each household member links their Google account once" not in text
    assert "Reference copy of the 4-step setup" not in text


def test_account_name_is_html_escaped():
    # A crafted name must not break out of the attribute / inject markup.
    acct = google_setup.GoogleAccount(
        name="abc", token_path="/z", email="<script>alert(1)</script>",
    )
    registry = SimpleNamespace(accounts=[acct], default_name="abc")
    text = google_setup._management_html(registry, REDIRECT, GOOD_CLIENT_ID, CSRF).decode()
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_connection_details_client_id_not_in_inline_js():
    # The reveal value rides in data-full, not in an inline script literal.
    text = google_setup._connection_details_html(GOOD_CLIENT_ID)
    assert 'data-action="reveal-client-id"' in text
    assert "data-full=" in text
    assert "<script>" not in text


# ---------------------------------------------------------------------------
# Routing + behaviour preserved
#
# We patch the `_common` plumbing the handler calls so the test never touches
# the network, filesystem, systemd, or real CSRF cookies. The handler logic
# (route dispatch, state selection, OAuth exchange wiring) is what's exercised.
# ---------------------------------------------------------------------------


class _FakeHandler:
    """Minimal stand-in for the request I/O the wizard's dispatcher and
    route bodies touch. The route bodies are closures over `cfg`, so only
    `do_GET` / `do_POST` are bound onto this instance; everything below the
    dispatcher runs for real (including the CSRF guard `@form_guarded`
    applies), which is why POSTs carry a real body and cookie."""

    def __init__(self, path: str, body: bytes = b"", cookie: str = ""):
        self.path = path
        self.headers = Message()
        self.headers["Host"] = "jts.local"
        if body:
            self.headers["Content-Type"] = "application/x-www-form-urlencoded"
            self.headers["Content-Length"] = str(len(body))
        if cookie:
            self.headers["Cookie"] = f"{web_common.CSRF_COOKIE_NAME}={cookie}"
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.sent_html: list[bytes] = []
        self.redirects: list[str] = []
        self.errors: list[int] = []
        self.status: int | None = None
        # Only populated when a test leaves `send_see_other` unpatched, to
        # inspect the actual response headers (e.g. the Set-Cookie value).
        self.response_headers: list[tuple[str, str]] = []

    def address_string(self) -> str:  # used by log_message
        return "test"

    def send_error(self, code, *a, **k):
        self.status = int(code)
        self.errors.append(int(code))

    def send_response(self, status, *a, **k):
        self.status = int(status)

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        pass


def _bind(HandlerClass, path, *, body: bytes = b"", cookie: str = ""):
    """Return a _FakeHandler with the real do_GET/do_POST bound to it."""
    fake = _FakeHandler(path, body=body, cookie=cookie)
    for attr in ("do_GET", "do_POST"):
        setattr(fake, attr, getattr(HandlerClass, attr).__get__(fake, HandlerClass))
    return fake


def _make_bound_handler(cfg, path, *, body: bytes = b"", cookie: str = ""):
    return _bind(google_setup._make_handler(cfg), path, body=body, cookie=cookie)


def _form_body(form: dict[str, str] | None, *, token: str | None = CSRF) -> bytes:
    """Encode a form the way the rendered page posts it: the fields plus the
    hidden double-submit `csrf_token`. `token=None` omits the token, which is
    what an off-origin forgery looks like on the wire."""
    fields = dict(form or {})
    if token is not None:
        fields[web_common.CSRF_FORM_FIELD] = token
    return urllib.parse.urlencode(fields).encode()


def _post_handler(cfg, path, form=None, *, token: str | None = CSRF, cookie=CSRF):
    return _make_bound_handler(
        cfg, path, body=_form_body(form, token=token), cookie=cookie,
    )


def _write_creds(path, *, client_id=GOOD_CLIENT_ID, client_secret="secret"):
    path.write_text(
        f"GOOGLE_CLIENT_ID={client_id}\nGOOGLE_CLIENT_SECRET={client_secret}\n"
    )
    return str(path)


@pytest.fixture(autouse=True)
def _no_google_creds_env(monkeypatch):
    """Keep a value inherited from the developer's shell out of the
    file-freshness assertions. One test sets these back deliberately."""
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)


@pytest.fixture
def patched_common():
    """Patch the I/O surface the handler relies on. The CSRF guard is NOT
    patched — it moved into `_common.form_guarded`, so the POST pins below
    carry a real token and the rejection cases are asserted on the wire."""
    send_see_other = mock.Mock()
    with mock.patch.object(google_setup, "begin_request",
                           return_value={"csrf_token": CSRF, "flash": ""}) as br, \
         mock.patch.object(google_setup, "send_html_response") as shr, \
         mock.patch.object(google_setup, "send_see_other", send_see_other), \
         mock.patch.object(web_common, "send_see_other", send_see_other), \
         mock.patch.object(
             google_setup, "restart_voice_daemon",
             return_value=web_common.RestartOutcome.RAN,
         ) as rvd:
        yield SimpleNamespace(
            begin_request=br, send_html_response=shr,
            send_see_other=send_see_other, restart_voice_daemon=rvd,
        )


def _cfg(**over):
    base = {
        "creds_path": "/tmp/does-not-exist/google_credentials.env",
        "redirect_uri": REDIRECT,
        "registry_path": "/tmp/does-not-exist/accounts.json",
    }
    base.update(over)
    return base


def _flash(send_see_other_mock) -> str:
    """Return the user-visible message from the most recent send_see_other
    call. Most routes call `send_see_other(self, "./", flash="…")`, so the
    text lands in the `flash` kwarg, not the URL. A direct
    `send_see_other(self, url)` (the OAuth-start redirect) has no flash;
    this returns the URL in that case so callers can match either."""
    call = send_see_other_mock.call_args
    if call.kwargs.get("flash"):
        return call.kwargs["flash"]
    # Fall back to the positional URL (args[0] is the handler, args[1] the URL).
    return call.args[1] if len(call.args) > 1 else ""


def test_get_root_renders_state1_when_no_creds(patched_common):
    cfg = _cfg()
    fake = _make_bound_handler(cfg, "/")
    fake.do_GET()
    assert patched_common.send_html_response.called
    page = patched_common.send_html_response.call_args.args[1]
    assert b"setup-steps" in page  # state 1


def test_get_root_with_tools_return_uses_tool_pack_back_link(patched_common):
    cfg = _cfg()
    fake = _make_bound_handler(
        cfg, "/?return_to=%2Fassistant%2Ftools%2Fpack%2Fgoogle%2F",
    )
    fake.do_GET()
    assert patched_common.send_html_response.called
    page = patched_common.send_html_response.call_args.args[1].decode()
    assert 'href="/assistant/tools/pack/google/"' in page


def test_get_root_rejects_off_origin_return_link(patched_common):
    cfg = _cfg()
    fake = _make_bound_handler(cfg, "/?return_to=%2F%2Fevil.test%2F")
    fake.do_GET()
    assert patched_common.send_html_response.called
    page = patched_common.send_html_response.call_args.args[1].decode()
    assert 'href="/assistant/"' in page
    assert "evil.test" not in page


def test_get_root_renders_state2_when_creds_no_accounts(patched_common, tmp_path):
    cfg = _cfg(creds_path=_write_creds(tmp_path / "creds.env"))
    fake = _make_bound_handler(cfg, "/")
    with mock.patch.object(google_setup.GoogleRegistry, "load",
                           return_value=SimpleNamespace(accounts=[], default_name=None)):
        fake.do_GET()
    page = patched_common.send_html_response.call_args.args[1]
    assert b'action="start"' in page  # state 2: add-account form


def test_get_root_renders_state3_when_accounts(patched_common, tmp_path):
    cfg = _cfg(creds_path=_write_creds(tmp_path / "creds.env"))
    acct = google_setup.GoogleAccount(name="jasper", token_path="/x", email="j@x")
    fake = _make_bound_handler(cfg, "/")
    with mock.patch.object(google_setup.GoogleRegistry, "load",
                           return_value=SimpleNamespace(accounts=[acct], default_name="jasper")):
        fake.do_GET()
    page = patched_common.send_html_response.call_args.args[1]
    assert b'class="accounts"' in page  # state 3


def test_unknown_get_404s(patched_common):
    fake = _make_bound_handler(_cfg(), "/bogus")
    fake.do_GET()
    assert 404 in fake.errors


def test_unknown_post_404s_before_csrf(patched_common):
    fake = _post_handler(_cfg(), "/bogus", {"client_id": "x"})
    fake.do_POST()
    assert 404 in fake.errors
    # The route check runs first, so the body is still unread: an unknown
    # path can neither consume the request nor reveal the CSRF state.
    assert fake.rfile.tell() == 0


def test_post_bad_csrf_rejected(patched_common):
    fake = _post_handler(
        _cfg(), "/setup-credentials",
        {"client_id": GOOD_CLIENT_ID, "client_secret": "GOCSPX-abc"},
        token=None,
    )
    with mock.patch.object(google_setup, "_write_creds_file") as wcf:
        fake.do_POST()
    assert fake.status == int(http.HTTPStatus.FORBIDDEN)
    assert b"Session expired" in fake.wfile.getvalue()
    assert not wcf.called  # the route body never ran


def test_setup_credentials_rejects_bad_client_id(patched_common):
    fake = _post_handler(_cfg(), "/setup-credentials", {
        "client_id": "not-a-google-id", "client_secret": "GOCSPX-abc",
    })
    with mock.patch.object(google_setup, "_write_creds_file") as wcf:
        fake.do_POST()
    # Redirected with a validation message; creds NOT persisted.
    assert patched_common.send_see_other.called
    assert not wcf.called


def test_setup_credentials_persists_and_restarts(patched_common, tmp_path):
    cfg = _cfg(creds_path=str(tmp_path / "creds.env"))
    fake = _post_handler(cfg, "/setup-credentials", {
        "client_id": GOOD_CLIENT_ID, "client_secret": "GOCSPX-abc",
    })
    with mock.patch.object(google_setup, "_write_creds_file") as wcf:
        fake.do_POST()
    assert wcf.call_args.args == (GOOD_CLIENT_ID, "GOCSPX-abc")
    assert wcf.call_args.kwargs["path"] == cfg["creds_path"]
    assert patched_common.restart_voice_daemon.called


def test_reset_credentials_deletes_creds_file(patched_common, tmp_path):
    """D.7: this route used to build `./?msg=Credentials+cleared.`; it must
    now redirect to a clean `./` with the message in the flash cookie."""
    cfg = _cfg(creds_path=_write_creds(tmp_path / "creds.env"))
    fake = _post_handler(cfg, "/reset-credentials")
    with mock.patch.object(google_setup, "_delete_creds_file") as dcf:
        fake.do_POST()
    assert dcf.call_args.args == (cfg["creds_path"],)
    assert patched_common.restart_voice_daemon.called
    location = patched_common.send_see_other.call_args.args[1]
    assert location == "./"
    assert "msg=" not in location
    assert patched_common.send_see_other.call_args.kwargs["flash"] == (
        "Credentials cleared." + web_common.RESTART_CLAUSE[web_common.RestartOutcome.RAN]
    )


@pytest.mark.parametrize("outcome", list(web_common.RestartOutcome))
def test_reset_credentials_describes_the_restart_it_actually_got(
    patched_common, tmp_path, outcome,
):
    patched_common.restart_voice_daemon.return_value = outcome
    cfg = _cfg(creds_path=_write_creds(tmp_path / "creds.env"))
    fake = _post_handler(cfg, "/reset-credentials")
    with mock.patch.object(google_setup, "_delete_creds_file"):
        fake.do_POST()
    flash = patched_common.send_see_other.call_args.kwargs["flash"]
    assert flash == "Credentials cleared." + web_common.RESTART_CLAUSE[outcome]


def test_setup_credentials_failure_flashes_instead_of_raising(patched_common, tmp_path):
    # `write_env_file` refuses a value carrying a newline (it would split the
    # file into a bogus second line), so a pasted secret can reach the save
    # site as a ValueError, not only an OSError.
    creds = tmp_path / "creds.env"
    cfg = _cfg(creds_path=str(creds))
    _post_handler(cfg, "/setup-credentials", {
        "client_id": GOOD_CLIENT_ID, "client_secret": "abc\ndef",
    }).do_POST()

    assert patched_common.send_see_other.call_args.args[1] == "./"
    assert _flash(patched_common.send_see_other)
    assert not creds.exists()
    assert not patched_common.restart_voice_daemon.called


def test_reset_credentials_failure_does_not_report_a_cleared_secret(
    patched_common, tmp_path,
):
    # `_common.delete_env_file` is warn-and-continue; a reset that did not
    # delete must not flash success while the wizard still renders the creds.
    failure = OSError(13, "Permission denied")
    cfg = _cfg(creds_path=_write_creds(tmp_path / "creds.env"),
               registry_path=str(tmp_path / "accounts.json"))
    with mock.patch.object(google_setup.os, "unlink", side_effect=failure):
        _post_handler(cfg, "/reset-credentials").do_POST()

    assert _flash(patched_common.send_see_other)
    assert not patched_common.restart_voice_daemon.called

    _make_bound_handler(cfg, "/").do_GET()
    page = patched_common.send_html_response.call_args.args[1]
    assert b'action="start"' in page  # state 2: credentials still present


def test_reset_beats_the_systemd_env_snapshot(patched_common, tmp_path, monkeypatch):
    # An inherited `GOOGLE_CLIENT_*` (an operator's shell, or a unit that
    # sourced the file at start) is a copy that outlives the delete — read it
    # and "clear credentials" would not take effect until the next restart.
    monkeypatch.setenv("GOOGLE_CLIENT_ID", GOOD_CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    creds = tmp_path / "creds.env"
    cfg = _cfg(creds_path=_write_creds(creds),
               registry_path=str(tmp_path / "accounts.json"))

    _post_handler(cfg, "/reset-credentials").do_POST()
    assert not creds.exists()

    _make_bound_handler(cfg, "/").do_GET()
    page = patched_common.send_html_response.call_args.args[1]
    assert b"setup-steps" in page  # state 1: paste credentials
    assert GOOD_CLIENT_ID.encode() not in page

    with mock.patch.object(google_setup, "_build_flow") as bf:
        _post_handler(cfg, "/start", {"name": "jasper"}).do_POST()
    assert not bf.called


def test_start_redirects_to_google_authorize(patched_common, tmp_path):
    cfg = _cfg(creds_path=_write_creds(tmp_path / "creds.env"))
    fake = _post_handler(cfg, "/start", {"name": "jasper"})
    fake_registry = mock.Mock()
    fake_flow = SimpleNamespace(
        authorization_url=lambda **k: ("https://accounts.google.com/o/oauth2/auth?x=1", "jasper"),
        code_verifier="verifier123",
    )
    google_setup._PENDING_FLOWS.clear()
    with mock.patch.object(google_setup.GoogleRegistry, "load", return_value=fake_registry), \
         mock.patch.object(google_setup, "default_token_path_for", return_value="/tok"), \
         mock.patch.object(google_setup, "_build_flow", return_value=fake_flow):
        fake.do_POST()
    # Redirected to the Google authorize URL; the PKCE verifier + account
    # name are stashed under an unguessable CSRF nonce (not the account name).
    loc = patched_common.send_see_other.call_args.args[1]
    assert loc.startswith("https://accounts.google.com/o/oauth2/auth")
    pending = list(google_setup._PENDING_FLOWS.items())
    assert len(pending) == 1
    nonce, (name, verifier, _created) = pending[0]
    assert nonce != "jasper"  # not the predictable account name
    assert len(nonce) >= 16  # token_urlsafe(16) → unguessable
    assert (name, verifier) == ("jasper", "verifier123")


def test_start_uses_creds_rewritten_under_a_running_server(patched_common, tmp_path):
    # Any other writer of the creds file (install migration, restore, hand
    # edit) must be visible without restarting jasper-web.
    creds = tmp_path / "creds.env"
    _write_creds(creds)
    server = google_setup.make_server(
        ("127.0.0.1", 0),
        registry_path=str(tmp_path / "accounts.json"),
        redirect_uri=REDIRECT,
        creds_path=str(creds),
    )
    server.server_close()
    rotated = "999999999999-rotated.apps.googleusercontent.com"
    _write_creds(creds, client_id=rotated)
    fake = _bind(
        server.RequestHandlerClass, "/start",
        body=_form_body({"name": "jasper"}), cookie=CSRF,
    )
    google_setup._PENDING_FLOWS.clear()
    with mock.patch.object(google_setup.GoogleRegistry, "load",
                           return_value=mock.Mock()), \
         mock.patch.object(google_setup, "default_token_path_for",
                           return_value="/tok"):
        fake.do_POST()

    loc = patched_common.send_see_other.call_args.args[1]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    assert query["client_id"] == [rotated]


def test_start_rejects_bad_name(patched_common, tmp_path):
    cfg = _cfg(creds_path=_write_creds(tmp_path / "creds.env"))
    fake = _post_handler(cfg, "/start", {"name": "has spaces!"})
    fake.do_POST()
    assert "Invalid name" in _flash(patched_common.send_see_other)


def test_default_sets_default_when_account_exists(patched_common):
    fake = _post_handler(_cfg(), "/default", {"name": "britt"})
    reg = mock.Mock()
    reg.get.return_value = object()  # account exists
    with mock.patch.object(google_setup.GoogleRegistry, "load", return_value=reg):
        fake.do_POST()
    assert reg.default_name == "britt"
    assert reg.save.called


def test_remove_deletes_account_and_token(patched_common, tmp_path):
    fake = _post_handler(_cfg(), "/remove", {"name": "jasper"})
    tok = tmp_path / "jasper.json"
    tok.write_text("{}")
    reg = mock.Mock()
    reg.get.return_value = SimpleNamespace(token_path=str(tok))
    reg.remove.return_value = True
    with mock.patch.object(google_setup.GoogleRegistry, "load", return_value=reg):
        fake.do_POST()
    assert reg.remove.called
    assert not tok.exists()  # token file unlinked
    assert patched_common.restart_voice_daemon.called


def test_callback_exchanges_code_and_restarts(patched_common, tmp_path):
    cfg = _cfg(creds_path=_write_creds(tmp_path / "creds.env"))
    # Seed a pending flow as /start would: nonce → (account, verifier, ts).
    google_setup._PENDING_FLOWS.clear()
    google_setup._PENDING_FLOWS["nonce123"] = ("jasper", "verifier123", 0.0)
    fake = _make_bound_handler(cfg, "/callback?code=abc&state=nonce123")
    flow = SimpleNamespace(
        code_verifier=None,
        fetch_token=mock.Mock(),
        credentials=SimpleNamespace(
            refresh_token="refresh", scopes=None, token_uri=None, token="",
        ),
    )
    reg = mock.Mock()
    reg.get.return_value = SimpleNamespace(token_path=str(tmp_path / "tok.json"))
    with mock.patch.object(google_setup.GoogleRegistry, "load", return_value=reg), \
         mock.patch.object(google_setup, "_build_flow", return_value=flow) as bf, \
         mock.patch.object(google_setup, "save_token") as st, \
         mock.patch.object(google_setup, "_fetch_userinfo", return_value={}), \
         mock.patch.object(google_setup, "_gc_pending"):  # don't expire our 0.0 ts
        fake.do_GET()
    # Nonce resolved to the account name + stashed PKCE verifier, and the
    # creds read for this request ride along (the file is read once).
    assert bf.call_args.args[1] == (GOOD_CLIENT_ID, "secret")
    assert bf.call_args.kwargs["state"] == "jasper"
    assert flow.code_verifier == "verifier123"
    assert flow.fetch_token.call_args.kwargs == {"code": "abc"}
    assert st.call_args.args == (str(tmp_path / "tok.json"),)
    # Nonce consumed (single-use).
    assert "nonce123" not in google_setup._PENDING_FLOWS
    assert patched_common.restart_voice_daemon.called
    # Redirected back to / with a success flash: "Linked …" lands in the
    # flash kwarg, and the URL is the plain "./".
    assert "Linked" in _flash(patched_common.send_see_other)


def test_callback_exchange_failure_flash_is_redacted(patched_common, tmp_path, caplog):
    # The token endpoint's rejection text reaches the user scrubbed and
    # bounded; the flash-cookie shim already kept it out of the URL. The
    # journal line beside the flash gets the same scrubbing — nothing
    # upgrades the raw provider rejection to a traceback dump.
    cfg = _cfg(creds_path=_write_creds(tmp_path / "creds.env"))
    google_setup._PENDING_FLOWS.clear()
    google_setup._PENDING_FLOWS["nonce123"] = ("jasper", "verifier123", 0.0)
    fake = _make_bound_handler(cfg, "/callback?code=abc&state=nonce123")
    leaked = "GOCSPX-fakefake1234"
    with mock.patch.object(google_setup.GoogleRegistry, "load",
                           return_value=mock.Mock()), \
        mock.patch.object(
            google_setup, "_build_flow",
            side_effect=RuntimeError(f"400 invalid_client: client_secret={leaked}"),
    ), mock.patch.object(google_setup, "_gc_pending"), caplog.at_level(
        logging.WARNING, logger="jasper.web.google_setup"
    ):
        fake.do_GET()

    assert patched_common.send_see_other.call_args.args[1] == "./"
    assert leaked not in _flash(patched_common.send_see_other)
    assert leaked not in caplog.text
    assert not patched_common.restart_voice_daemon.called


def test_callback_rejects_unknown_state_without_exchange(patched_common, tmp_path):
    # CSRF guard: a forged callback with a state that was never issued
    # (or already consumed / expired) must not run the token exchange.
    cfg = _cfg(creds_path=_write_creds(tmp_path / "creds.env"))
    google_setup._PENDING_FLOWS.clear()
    fake = _make_bound_handler(cfg, "/callback?code=abc&state=forged")
    with mock.patch.object(google_setup, "_build_flow") as bf:
        fake.do_GET()
        assert not bf.called
    assert not patched_common.restart_voice_daemon.called
    assert "expired" in _flash(patched_common.send_see_other)


def test_callback_with_error_redirects_without_exchange(patched_common):
    fake = _make_bound_handler(_cfg(), "/callback?error=access_denied")
    with mock.patch.object(google_setup, "_build_flow") as bf:
        fake.do_GET()
        assert not bf.called
    assert patched_common.send_see_other.called


def test_callback_with_long_error_caps_the_flash_cookie():
    # An unauthenticated GET can carry an arbitrary ?error= value. It must
    # be redacted and length-capped the same as any exception-derived
    # flash (google_setup routes it through the same `flash_error` helper),
    # so it can't inflate the Set-Cookie header. `send_see_other` is left
    # unpatched here so the real cookie-building code runs end to end.
    fake = _make_bound_handler(_cfg(), "/callback?error=" + "x" * 2000)
    with mock.patch.object(google_setup, "_build_flow") as bf:
        fake.do_GET()
        assert not bf.called
    cookie = next(v for n, v in fake.response_headers if n == "Set-Cookie")
    flash = urllib.parse.unquote(cookie.split(";", 1)[0].split("=", 1)[1])
    assert len(flash) <= len("Google returned error: ") + web_common._FLASH_DETAIL_CAP


def test_the_callback_access_log_never_carries_the_authorization_code(caplog):
    """Google returns the single-use authorization code as `?code=…` on the
    callback request line, which the stdlib hands to `log_message` verbatim.
    The query string is dropped before the record exists, so the code cannot
    reach the journal even if no redaction pattern happened to match it."""
    handler_cls = google_setup._make_handler(_cfg())
    fake = _FakeHandler("/callback")
    log_message = handler_cls.log_message.__get__(fake, handler_cls)

    with caplog.at_level(logging.INFO, logger=google_setup.logger.name):
        log_message(
            '"%s" %s %s',
            "GET /callback?code=4-0AVMBsJgAbCdEf&state=xyz HTTP/1.1",
            "302",
            "-",
        )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "4-0AVMBsJgAbCdEf" not in logged
    assert "code=" not in logged
    assert "/callback" in logged  # the path itself still reaches the journal


# ----------------------------------------------------------------------
# The userinfo fetch carries the freshly-issued bearer token.
# ----------------------------------------------------------------------


@pytest.fixture()
def userinfo_servers():
    """A server that 302s to a second server, plus that second server's
    request log — an empty log proves the bearer never rode the second hop.
    The `/<n>` path on either serves n bytes of JSON padding."""
    hits: list[dict] = []

    def _serve(handler_cls):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    class _Sink(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            hits.append({k.lower(): v for k, v in self.headers.items()})
            body = b'{"email": "a@b.c", "pad": "%s"}' % (
                b"x" * max(0, int(self.path.lstrip("/") or 0))
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    sink = _serve(_Sink)
    sink_url = f"http://127.0.0.1:{sink.server_address[1]}/0"

    class _Redirector(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", sink_url)
            self.send_header("Content-Length", "0")
            self.end_headers()

    redirector = _serve(_Redirector)
    yield f"http://127.0.0.1:{redirector.server_address[1]}/", sink_url, hits
    for each in (redirector, sink):
        each.shutdown()
        each.server_close()


def test_the_userinfo_bearer_never_rides_a_redirect(
    monkeypatch, userinfo_servers,
):
    """urllib replays every request header onto a redirect, so following one
    would hand the access token to whatever host the reply named."""
    redirect_url, _, hits = userinfo_servers
    monkeypatch.setattr(google_setup, "_USERINFO_URI", redirect_url)

    assert google_setup._fetch_userinfo("ya29.secret-access-token") == {}
    assert hits == []


def test_an_oversized_userinfo_reply_is_not_decoded(
    monkeypatch, userinfo_servers,
):
    """A claims object is small; a body past the cap must not be read whole
    onto a 415 MB box."""
    _, sink_url, _ = userinfo_servers
    base = sink_url.rsplit("/", 1)[0]
    cap = google_setup._USERINFO_MAX_BYTES

    monkeypatch.setattr(google_setup, "_USERINFO_URI", f"{base}/0")
    assert google_setup._fetch_userinfo("ya29.token")["email"] == "a@b.c"

    monkeypatch.setattr(google_setup, "_USERINFO_URI", f"{base}/{cap}")
    assert google_setup._fetch_userinfo("ya29.token") == {}
