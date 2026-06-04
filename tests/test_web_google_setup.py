"""Hardware-free tests for the migrated /google/ wizard.

Covers two things:

1. Canonical render — each of the three state renderers
   (`_setup_wizard_html`, `_redirect_uri_page_html`, `_management_html`)
   emits the canonical document shell: the `/assets/app.css` link, the
   `.app-header` top bar, the `<meta name="jts-csrf">` tag, the hidden
   `csrf_token` form field, the page's ES module, and the page CSS link.
   These also assert the migration removed the legacy chrome (no
   `wrap_page`/`PAGE_STYLE` markers, no inline `<script>` in the body, no
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
from types import SimpleNamespace
from unittest import mock

import pytest

google_setup = importlib.import_module("jasper.web.google_setup")


CSRF = "x" * 43
GOOD_CLIENT_ID = "123456789012-abcdefg.apps.googleusercontent.com"
REDIRECT = "https://jaspercurry.github.io/google-oauth-callback/?host=jts.local"


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_public_surface_present():
    assert callable(google_setup.make_server)
    assert callable(google_setup.main)
    # State renderers used by the handler's _render_index.
    assert callable(google_setup._setup_wizard_html)
    assert callable(google_setup._redirect_uri_page_html)
    assert callable(google_setup._management_html)
    assert callable(google_setup.default_redirect_uri)


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
    assert "PAGE_STYLE" not in text
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
    assert 'class="wizard-steps"' in text
    assert "data-step=\"4\"" in text


def test_redirect_uri_page_html_is_canonical():
    page = google_setup._redirect_uri_page_html(REDIRECT, GOOD_CLIENT_ID, CSRF)
    text = _assert_canonical(page)
    # State 2: add-account form + reset-credentials confirm guard.
    assert 'action="start"' in text
    assert 'action="reset-credentials"' in text
    assert "data-confirm" in text, "destructive confirm hook missing"
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
    # Default badge present with the OK status tone.
    assert "--tone: var(--status-ok)" in text


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
    """Minimal stand-in that satisfies the closures in google_setup's
    Handler methods. We bind the real methods to this instance so the
    routing/branch logic runs, but the I/O helpers are patched at the
    module level."""

    def __init__(self, path: str):
        self.path = path
        self.sent_html: list[bytes] = []
        self.redirects: list[str] = []
        self.errors: list[int] = []

    def address_string(self) -> str:  # used by log_message
        return "test"

    def send_error(self, code, *a, **k):
        self.errors.append(int(code))


def _make_bound_handler(cfg, path):
    """Return (fake, HandlerClass) with the real do_GET/do_POST/route bodies
    bound to a _FakeHandler instance."""
    HandlerClass = google_setup._make_handler(cfg)
    fake = _FakeHandler(path)
    # Bind the methods we exercise onto the fake instance.
    bound = {}
    for attr in dir(HandlerClass):
        fn = getattr(HandlerClass, attr)
        if callable(fn) and (attr.startswith("do_") or attr.startswith("_handle_")
                             or attr in {"_render_index", "_exchange_code",
                                         "_redirect", "_send_html"}):
            bound[attr] = fn.__get__(fake, HandlerClass)
    for name, m in bound.items():
        setattr(fake, name, m)
    return fake


@pytest.fixture
def patched_common():
    """Patch the I/O surface the handler relies on."""
    with mock.patch.object(google_setup, "begin_request",
                           return_value={"csrf_token": CSRF, "flash": ""}) as br, \
         mock.patch.object(google_setup, "send_html_response") as shr, \
         mock.patch.object(google_setup, "send_see_other") as sso, \
         mock.patch.object(google_setup, "read_form", return_value={}) as rf, \
         mock.patch.object(google_setup, "verify_csrf", return_value=True) as vc, \
         mock.patch.object(google_setup, "reject_csrf") as rc, \
         mock.patch.object(google_setup, "restart_voice_daemon") as rvd:
        yield SimpleNamespace(
            begin_request=br, send_html_response=shr, send_see_other=sso,
            read_form=rf, verify_csrf=vc, reject_csrf=rc, restart_voice_daemon=rvd,
        )


def _cfg(**over):
    base = {
        "client_id": "",
        "client_secret": "",
        "redirect_uri": REDIRECT,
        "registry_path": "/tmp/does-not-exist/accounts.json",
    }
    base.update(over)
    return base


def _flash(send_see_other_mock) -> str:
    """Return the user-visible message from the most recent send_see_other
    call. The handler routes most messages through `_redirect("./?msg=…")`,
    which the flash-cookie compat shim turns into
    `send_see_other(self, "./", flash="…")` — so the text lands in the
    `flash` kwarg, not the URL. A direct `send_see_other(self, url)` has no
    flash; this returns the URL in that case so callers can match either."""
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
    assert b"wizard-steps" in page  # state 1


def test_get_root_renders_state2_when_creds_no_accounts(patched_common):
    cfg = _cfg(client_id=GOOD_CLIENT_ID, client_secret="secret")
    fake = _make_bound_handler(cfg, "/")
    with mock.patch.object(google_setup.GoogleRegistry, "load",
                           return_value=SimpleNamespace(accounts=[], default_name=None)):
        fake.do_GET()
    page = patched_common.send_html_response.call_args.args[1]
    assert b'action="start"' in page  # state 2: add-account form


def test_get_root_renders_state3_when_accounts(patched_common):
    cfg = _cfg(client_id=GOOD_CLIENT_ID, client_secret="secret")
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
    fake = _make_bound_handler(_cfg(), "/bogus")
    fake.do_POST()
    assert 404 in fake.errors
    # verify_csrf must NOT be consulted for an unknown path.
    assert not patched_common.verify_csrf.called


def test_post_bad_csrf_rejected(patched_common):
    patched_common.verify_csrf.return_value = False
    fake = _make_bound_handler(_cfg(), "/setup-credentials")
    fake.do_POST()
    assert patched_common.reject_csrf.called


def test_setup_credentials_rejects_bad_client_id(patched_common):
    patched_common.read_form.return_value = {
        "client_id": "not-a-google-id", "client_secret": "GOCSPX-abc",
    }
    cfg = _cfg()
    fake = _make_bound_handler(cfg, "/setup-credentials")
    fake.do_POST()
    # Redirected with a validation message; creds NOT persisted into cfg.
    assert patched_common.send_see_other.called
    assert cfg["client_id"] == ""


def test_setup_credentials_persists_and_restarts(patched_common, tmp_path):
    patched_common.read_form.return_value = {
        "client_id": GOOD_CLIENT_ID, "client_secret": "GOCSPX-abc",
    }
    cfg = _cfg()
    fake = _make_bound_handler(cfg, "/setup-credentials")
    tmp_path / "google_credentials.env"
    with mock.patch.object(google_setup, "_write_creds_file") as wcf:
        fake.do_POST()
        assert wcf.called
    assert cfg["client_id"] == GOOD_CLIENT_ID
    assert cfg["client_secret"] == "GOCSPX-abc"
    assert patched_common.restart_voice_daemon.called


def test_reset_credentials_clears_cfg(patched_common):
    cfg = _cfg(client_id=GOOD_CLIENT_ID, client_secret="secret")
    fake = _make_bound_handler(cfg, "/reset-credentials")
    with mock.patch.object(google_setup, "_delete_creds_file") as dcf:
        fake.do_POST()
        assert dcf.called
    assert cfg["client_id"] == ""
    assert cfg["client_secret"] == ""
    assert patched_common.restart_voice_daemon.called


def test_start_redirects_to_google_authorize(patched_common):
    patched_common.read_form.return_value = {"name": "jasper"}
    cfg = _cfg(client_id=GOOD_CLIENT_ID, client_secret="secret")
    fake = _make_bound_handler(cfg, "/start")
    fake_registry = mock.Mock()
    fake_flow = SimpleNamespace(
        authorization_url=lambda **k: ("https://accounts.google.com/o/oauth2/auth?x=1", "jasper"),
        code_verifier="verifier123",
    )
    with mock.patch.object(google_setup.GoogleRegistry, "load", return_value=fake_registry), \
         mock.patch.object(google_setup, "default_token_path_for", return_value="/tok"), \
         mock.patch.object(google_setup, "_build_flow", return_value=fake_flow):
        fake.do_POST()
    # Redirected to the Google authorize URL; PKCE verifier stashed in cfg.
    loc = patched_common.send_see_other.call_args.args[1]
    assert loc.startswith("https://accounts.google.com/o/oauth2/auth")
    assert cfg["pending_verifiers"]["jasper"] == "verifier123"


def test_start_rejects_bad_name(patched_common):
    patched_common.read_form.return_value = {"name": "has spaces!"}
    cfg = _cfg(client_id=GOOD_CLIENT_ID, client_secret="secret")
    fake = _make_bound_handler(cfg, "/start")
    fake.do_POST()
    # The route calls self._redirect("./?msg=Invalid+name…"); the flash-cookie
    # compat shim splits that into send_see_other(self, "./", flash="Invalid name…")
    # — so the human message lands in the `flash` kwarg, not the URL.
    assert "Invalid name" in _flash(patched_common.send_see_other)


def test_default_sets_default_when_account_exists(patched_common):
    patched_common.read_form.return_value = {"name": "britt"}
    cfg = _cfg(client_id=GOOD_CLIENT_ID, client_secret="secret")
    fake = _make_bound_handler(cfg, "/default")
    reg = mock.Mock()
    reg.get.return_value = object()  # account exists
    with mock.patch.object(google_setup.GoogleRegistry, "load", return_value=reg):
        fake.do_POST()
    assert reg.default_name == "britt"
    assert reg.save.called


def test_remove_deletes_account_and_token(patched_common, tmp_path):
    patched_common.read_form.return_value = {"name": "jasper"}
    cfg = _cfg(client_id=GOOD_CLIENT_ID, client_secret="secret")
    fake = _make_bound_handler(cfg, "/remove")
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


def test_callback_exchanges_code_and_restarts(patched_common):
    cfg = _cfg(client_id=GOOD_CLIENT_ID, client_secret="secret")
    fake = _make_bound_handler(cfg, "/callback?code=abc&state=jasper")
    with mock.patch.object(fake, "_exchange_code") as ex:
        fake.do_GET()
        assert ex.called
        assert ex.call_args.args == ("jasper", "abc")
    assert patched_common.restart_voice_daemon.called
    # Redirected back to / with a success flash (via the _redirect shim, so the
    # "Linked …" text is in the flash kwarg, and the URL is the cleaned "./").
    assert "Linked" in _flash(patched_common.send_see_other)


def test_callback_with_error_redirects_without_exchange(patched_common):
    cfg = _cfg(client_id=GOOD_CLIENT_ID, client_secret="secret")
    fake = _make_bound_handler(cfg, "/callback?error=access_denied")
    with mock.patch.object(fake, "_exchange_code") as ex:
        fake.do_GET()
        assert not ex.called
    assert patched_common.send_see_other.called
