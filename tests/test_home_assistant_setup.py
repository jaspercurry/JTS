"""Tests for the /ha/ wizard.

What's exercised:
  - URL normalization (scheme/port defaulting, trailing-slash/api-suffix
    stripping)
  - Three-state rendering (none / partial / connected) driven by the
    env file content
  - End-to-end handler round-trips for /save (state-1 URL-only,
    state-2 full submit), /reset, /disconnect
  - Recent-URLs persistence (push-to-front, dedupe, cap at 3)
  - mocked verify() success + failure paths drive the right state file
    transitions
  - JSON endpoint shapes (/discover, /verify)

Network calls to HA are mocked by monkeypatching the verify_sync
function; the mDNS scanner returns [] in tests (no real LAN browse).
`restart_voice_daemon` is patched so we don't shell out to systemctl.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from jasper.web import home_assistant_setup as ha_setup


# ---- URL normalization ----------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("homeassistant.local", "http://homeassistant.local:8123"),
        ("homeassistant.local:8123", "http://homeassistant.local:8123"),
        ("homeassistant.local:8123/", "http://homeassistant.local:8123"),
        ("http://homeassistant.local:8123/", "http://homeassistant.local:8123"),
        ("http://homeassistant.local:8123/api", "http://homeassistant.local:8123"),
        ("http://homeassistant.local:8123/api/", "http://homeassistant.local:8123"),
        ("192.168.1.42:8123", "http://192.168.1.42:8123"),
        ("192.168.1.42", "http://192.168.1.42:8123"),
        ("https://my-ha.example.com", "https://my-ha.example.com"),
        ("https://my-ha.example.com:8443/api", "https://my-ha.example.com:8443"),
        ("  http://x:8123  ", "http://x:8123"),
        ("", ""),
    ],
)
def test_normalize_url(raw, expected):
    assert ha_setup._normalize_url(raw) == expected


# ---- Recent-URLs persistence ----------------------------------------------

def test_recent_urls_empty_when_unset():
    assert ha_setup._recent_urls({}) == []


def test_recent_urls_returns_parsed_list():
    state = {ha_setup.ENV_RECENT_URLS: json.dumps(["http://a:8123", "http://b:8123"])}
    assert ha_setup._recent_urls(state) == ["http://a:8123", "http://b:8123"]


def test_recent_urls_handles_garbage_value_gracefully():
    assert ha_setup._recent_urls({ha_setup.ENV_RECENT_URLS: "not-json"}) == []
    assert ha_setup._recent_urls({ha_setup.ENV_RECENT_URLS: '"a-string-not-list"'}) == []


def test_recent_urls_capped_at_max():
    state = {ha_setup.ENV_RECENT_URLS: json.dumps([f"http://{i}" for i in range(10)])}
    assert len(ha_setup._recent_urls(state)) == ha_setup.RECENT_URLS_MAX


def test_push_recent_url_moves_to_front():
    out = ha_setup._push_recent_url(["a", "b", "c"], "b")
    assert out == ["b", "a", "c"]


def test_push_recent_url_dedupes_new_first():
    out = ha_setup._push_recent_url(["a", "b"], "c")
    assert out == ["c", "a", "b"]


def test_push_recent_url_caps_at_max():
    out = ha_setup._push_recent_url(["a", "b", "c"], "d")
    assert out == ["d", "a", "b"]
    assert len(out) == ha_setup.RECENT_URLS_MAX


# ---- State machine --------------------------------------------------------

def test_state_machine_none_when_empty():
    assert ha_setup._state_machine({}) == "none"


def test_state_machine_partial_when_url_no_token():
    assert ha_setup._state_machine({ha_setup.ENV_URL: "http://x:8123"}) == "partial"


def test_state_machine_connected_when_url_and_token():
    assert ha_setup._state_machine({
        ha_setup.ENV_URL: "http://x:8123",
        ha_setup.ENV_TOKEN: "abc",
    }) == "connected"


# ---- Profile link ---------------------------------------------------------

def test_profile_link_appended_to_url():
    assert ha_setup._profile_link("http://x:8123") == "http://x:8123/profile/security"


def test_profile_link_empty_for_empty_url():
    assert ha_setup._profile_link("") == ""


# ---- Handler end-to-end ----------------------------------------------------

@pytest.fixture
def wizard_server(tmp_path, monkeypatch):
    """Spin up the wizard on an ephemeral port for an integration-style
    round-trip. Mocks verify_sync so the test never hits the network,
    and captures restart_voice_daemon calls."""
    state_path = str(tmp_path / "home_assistant.env")

    restarts: list[None] = []
    monkeypatch.setattr(
        ha_setup, "restart_voice_daemon",
        lambda: restarts.append(None),
    )
    # Default verify mock: success. Individual tests override.
    monkeypatch.setattr(
        ha_setup, "verify_sync",
        lambda url, token, *, verify_ssl=True: {
            "ok": True, "url": url, "instance_name": "Home", "version": "2026.5.1",
            "agents": [{"entity_id": "conversation.home_assistant", "name": "Home Assistant"}],
        },
    )
    # mDNS scanner returns [] in tests (no live LAN browse).
    monkeypatch.setattr(ha_setup, "discover_sync", lambda timeout=None: [])

    server = ha_setup.make_server(("127.0.0.1", 0), state_path=state_path)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    port = server.server_address[1]
    try:
        yield (f"http://127.0.0.1:{port}", state_path, restarts)
    finally:
        server.shutdown()
        server.server_close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop urllib from auto-following 3xx. We want to assert on the
    redirect's status code + Location header explicitly."""
    def http_error_303(self, req, fp, code, msg, headers):  # noqa: ARG002
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
    http_error_301 = http_error_302 = http_error_307 = http_error_308 = http_error_303


_opener = urllib.request.build_opener(_NoRedirect())


def _get(url: str) -> tuple[int, str]:
    try:
        with _opener.open(url) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode() if hasattr(e, "read") else ""


def _post(url: str, form: dict) -> tuple[int, str, str | None]:
    """Returns (status, body, location_header).

    Mints the CSRF cookie via a GET to the wizard root, attaches it to
    the POST, and adds the csrf_token form field. Unknown POST routes
    still 404 (route check fires before CSRF check)."""
    import http.cookiejar
    from ._web_test_helpers import CSRF_COOKIE_NAME, CSRF_FORM_FIELD

    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    jar = http.cookiejar.CookieJar()
    cookie_opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPCookieProcessor(jar),
    )
    try:
        cookie_opener.open(base + "/").read()
    except urllib.error.HTTPError:
        pass
    token = ""
    for cookie in jar:
        if cookie.name == CSRF_COOKIE_NAME:
            token = cookie.value
            break

    payload = dict(form)
    if token:
        payload[CSRF_FORM_FIELD] = token
    body = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")

    def _maybe_append_flash(loc: str | None) -> str | None:
        # Tests previously asserted that the flash message appeared in
        # the Location header (because the wizard redirected to
        # ./?msg=…). The flash now travels in jts_flash cookie; surface
        # it after the location's `#` so the same string-contains
        # assertions keep working.
        for cookie in jar:
            if cookie.name == "jts_flash":
                flash = urllib.parse.unquote(cookie.value or "")
                if not loc:
                    return f"#{flash}"
                return f"{loc}#{flash}"
        return loc

    try:
        with cookie_opener.open(req) as r:
            return r.status, r.read().decode(), _maybe_append_flash(r.headers.get("Location"))
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") if e.headers else None
        loc = _maybe_append_flash(loc)
        return e.code, e.read().decode() if hasattr(e, "read") else "", loc


def test_get_cold_renders_state_none(wizard_server):
    base_url, _, _ = wizard_server
    status, body = _get(f"{base_url}/")
    assert status == 200
    assert "Find Home Assistant on this network" in body
    assert "manual" in body.lower() or "manually" in body.lower()


def test_save_url_only_transitions_to_state_partial(wizard_server):
    base_url, state_path, restarts = wizard_server
    status, _, loc = _post(f"{base_url}/save", {
        "url": "homeassistant.local:8123",
        "token": "",
        "agent_id": "",
    })
    assert status == 303
    assert loc == "./"
    # Env file should have URL only, no token.
    saved = _read_env(state_path)
    assert saved[ha_setup.ENV_URL] == "http://homeassistant.local:8123"
    assert ha_setup.ENV_TOKEN not in saved
    # No daemon restart for a partial save (token still missing).
    assert restarts == []


def test_get_after_url_save_renders_state_partial(wizard_server):
    base_url, state_path, _ = wizard_server
    _post(f"{base_url}/save", {"url": "homeassistant.local", "token": "", "agent_id": ""})
    status, body = _get(f"{base_url}/")
    assert status == 200
    # State 2 should show the URL and the LLAT paste field
    assert "homeassistant.local:8123" in body
    assert "Long-Lived Access Token" in body
    assert "/profile/security" in body


def test_save_full_round_trip_writes_env_and_restarts(wizard_server):
    base_url, state_path, restarts = wizard_server
    status, _, loc = _post(f"{base_url}/save", {
        "url": "homeassistant.local",
        "token": "eyJ0eXAi.test-token",
        "agent_id": "",
    })
    assert status == 303
    assert "Connected to Home" in urllib.parse.unquote(loc)
    # restarting=1 marker lets the connected page show its
    # "Configuring…" UX instead of letting the user immediately
    # try voice commands against a still-rebooting daemon.
    assert "restarting=1" in loc
    saved = _read_env(state_path)
    assert saved[ha_setup.ENV_URL] == "http://homeassistant.local:8123"
    assert saved[ha_setup.ENV_TOKEN] == "eyJ0eXAi.test-token"
    # Recent URLs has the saved URL at the front
    recent = json.loads(saved[ha_setup.ENV_RECENT_URLS])
    assert recent == ["http://homeassistant.local:8123"]
    # Daemon restarted on successful save
    assert len(restarts) == 1


def test_verify_ssl_env_var_constant_matches_module():
    """The wizard re-exports ENV_* constants from jasper.home_assistant
    rather than defining its own — that's the staff-review fix for
    env-var-string duplication. Confirm the re-export holds and the
    new VERIFY_SSL constant is in the chain."""
    import jasper.home_assistant as ha_mod
    assert ha_setup.ENV_URL == ha_mod.ENV_URL == "JASPER_HA_URL"
    assert ha_setup.ENV_TOKEN == ha_mod.ENV_TOKEN == "JASPER_HA_TOKEN"
    assert ha_setup.ENV_AGENT_ID == ha_mod.ENV_AGENT_ID == "JASPER_HA_AGENT_ID"
    assert ha_setup.ENV_VERIFY_SSL == ha_mod.ENV_VERIFY_SSL == "JASPER_HA_VERIFY_SSL"
    assert ha_setup.ENV_RECENT_URLS == ha_mod.ENV_RECENT_URLS == "JASPER_HA_RECENT_URLS"


def test_save_persists_verify_ssl_off_when_user_accepts_self_signed(wizard_server):
    """The 'Accept a self-signed certificate' checkbox in state 2 is
    named `accept_self_signed`, NOT `verify_ssl`, so its checked state
    matches the user's intent.

    CHECKED = 'yes, I accept a self-signed cert' = relax TLS
    verification = JASPER_HA_VERIFY_SSL=0 in the env file.

    Form sends `accept_self_signed=on` + the hidden marker
    `accept_self_signed_present=1`."""
    base_url, state_path, _ = wizard_server
    _post(f"{base_url}/save", {
        "url": "https://homeassistant.local:8123",
        "token": "test-token",
        "agent_id": "",
        "accept_self_signed_present": "1",
        "accept_self_signed": "on",  # CHECKED
    })
    saved = _read_env(state_path)
    assert saved[ha_setup.ENV_VERIFY_SSL] == "0"


def test_save_keeps_verify_ssl_strict_when_user_rejects_self_signed(wizard_server):
    """Checkbox UNCHECKED = user did NOT opt into self-signed = strict
    TLS verification (the safe default). The env file should NOT have
    JASPER_HA_VERIFY_SSL=0 — absence = default safe."""
    base_url, state_path, _ = wizard_server
    _post(f"{base_url}/save", {
        "url": "https://homeassistant.local:8123",
        "token": "test-token",
        "agent_id": "",
        "accept_self_signed_present": "1",
        # `accept_self_signed` field absent — checkbox UNCHECKED
    })
    saved = _read_env(state_path)
    assert ha_setup.ENV_VERIFY_SSL not in saved


def test_save_omits_verify_ssl_when_default_safe(wizard_server):
    """When `accept_self_signed_present` is absent (state-1 URL-only
    form) AND no prior verify_ssl was saved, the env file should NOT
    contain the verify_ssl key — absent = default safe (verify enabled)."""
    base_url, state_path, _ = wizard_server
    _post(f"{base_url}/save", {
        "url": "homeassistant.local",
        "token": "test-token",
        "agent_id": "",
        # no accept_self_signed_present, no accept_self_signed
    })
    saved = _read_env(state_path)
    assert ha_setup.ENV_VERIFY_SSL not in saved


def test_state_partial_renders_checkbox_for_https_url(wizard_server):
    """HTTPS URLs show the self-signed-cert checkbox in state 2;
    plain HTTP doesn't (no TLS to verify)."""
    base_url, state_path, _ = wizard_server
    # State 2 — HTTPS URL, no token yet
    _post(f"{base_url}/save", {
        "url": "https://homeassistant.local:8123",
        "token": "",
        "agent_id": "",
    })
    _, body = _get(f"{base_url}/")
    assert "Accept a self-signed certificate" in body
    # The form field is named `accept_self_signed`, not `verify_ssl`,
    # so a CHECKED box posts the user's stated intent (accept).
    assert "accept_self_signed" in body
    assert "accept_self_signed_present" in body


def test_state_partial_omits_checkbox_for_http_url(wizard_server):
    """Plain HTTP URLs don't show the cert checkbox."""
    base_url, state_path, _ = wizard_server
    _post(f"{base_url}/save", {
        "url": "homeassistant.local",  # normalizes to http://
        "token": "",
        "agent_id": "",
    })
    _, body = _get(f"{base_url}/")
    assert "Accept a self-signed certificate" not in body


def test_save_with_invalid_token_keeps_url_drops_token(wizard_server, monkeypatch):
    base_url, state_path, restarts = wizard_server
    # Override verify to fail with an auth error
    monkeypatch.setattr(
        ha_setup, "verify_sync",
        lambda url, token, *, verify_ssl=True: {"ok": False, "error": "Token wasn't accepted."},
    )
    status, _, loc = _post(f"{base_url}/save", {
        "url": "homeassistant.local",
        "token": "bad-token",
        "agent_id": "",
    })
    assert status == 303
    assert "Token wasn't accepted" in urllib.parse.unquote(loc)
    saved = _read_env(state_path)
    # URL persisted, token dropped, daemon NOT restarted
    assert saved[ha_setup.ENV_URL] == "http://homeassistant.local:8123"
    assert ha_setup.ENV_TOKEN not in saved
    assert restarts == []


def test_save_rejects_garbage_url(wizard_server, monkeypatch):
    base_url, state_path, _ = wizard_server
    # Make sure verify never runs for an unparseable URL
    monkeypatch.setattr(
        ha_setup, "verify_sync",
        lambda url, token, *, verify_ssl=True: pytest.fail("verify should not be called"),
    )
    status, _, loc = _post(f"{base_url}/save", {
        "url": "://not-a-url",
        "token": "t",
        "agent_id": "",
    })
    assert status == 303
    msg = urllib.parse.unquote(loc)
    assert "Couldn't parse" in msg or "URL" in msg


def test_disconnect_clears_token_keeps_recent_urls(wizard_server):
    base_url, state_path, restarts = wizard_server
    # First connect successfully
    _post(f"{base_url}/save", {
        "url": "homeassistant.local",
        "token": "valid-token",
        "agent_id": "",
    })
    restarts.clear()
    # Disconnect
    status, _, _ = _post(f"{base_url}/disconnect", {})
    assert status == 303
    saved = _read_env(state_path)
    # URL + token gone, but recent URLs persisted
    assert ha_setup.ENV_URL not in saved
    assert ha_setup.ENV_TOKEN not in saved
    assert ha_setup.ENV_RECENT_URLS in saved
    # Daemon restarted to clear the in-memory HAClient
    assert len(restarts) == 1


def test_reset_clears_url_keeps_recent_urls(wizard_server):
    base_url, state_path, _ = wizard_server
    # Set up a partial state (URL only)
    _post(f"{base_url}/save", {"url": "homeassistant.local", "token": "", "agent_id": ""})
    # Connect successfully (to populate recent URLs)
    _post(f"{base_url}/save", {
        "url": "homeassistant.local",
        "token": "good-token",
        "agent_id": "",
    })
    # Reset
    status, _ = _get(f"{base_url}/reset")
    assert status == 303
    saved = _read_env(state_path)
    assert ha_setup.ENV_URL not in saved
    assert ha_setup.ENV_TOKEN not in saved
    assert ha_setup.ENV_RECENT_URLS in saved


def test_discover_endpoint_returns_json_list(wizard_server, monkeypatch):
    base_url, _, _ = wizard_server
    # Mock discovery to return one fake instance
    monkeypatch.setattr(ha_setup, "discover_sync", lambda timeout=None: [
        {
            "name": "Home._home-assistant._tcp.local.",
            "host": "abc-uuid.local.",
            "port": "8123",
            "location_name": "Home",
            "version": "2026.5.1",
            "url": "http://192.168.1.42:8123",
        },
    ])
    body = urllib.parse.urlencode({}).encode()
    req = urllib.request.Request(f"{base_url}/discover", data=body, method="POST")
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    assert "instances" in data
    assert len(data["instances"]) == 1
    assert data["instances"][0]["location_name"] == "Home"
    assert data["instances"][0]["url"] == "http://192.168.1.42:8123"


def test_verify_endpoint_uses_persisted_state(wizard_server):
    """POST /verify reads URL+token from the env file (not the request
    body) — used by the connected-state agent picker and Test button."""
    base_url, _, _ = wizard_server
    # Connect first
    _post(f"{base_url}/save", {
        "url": "homeassistant.local",
        "token": "good-token",
        "agent_id": "",
    })
    body = urllib.parse.urlencode({}).encode()
    req = urllib.request.Request(f"{base_url}/verify", data=body, method="POST")
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    assert data["ok"] is True
    assert data["instance_name"] == "Home"
    assert any(a["entity_id"] == "conversation.home_assistant" for a in data["agents"])


def test_ready_endpoint_returns_yes_when_ha_reachable(wizard_server, monkeypatch):
    """POST /ready is the cheap-poll variant used by the connected-state
    JS during the post-save restart window. One HA call (GET /api/)
    via HAClient.healthcheck, not three. Returns {ok: bool}."""
    # Replace the HAClient inside ready_sync with a stub that has a
    # successful healthcheck. (verify_sync uses its own httpx
    # AsyncClient; ready_sync goes through HAClient — different mock
    # surface.)
    monkeypatch.setattr(
        ha_setup, "ready_sync",
        lambda url, token, *, verify_ssl=True: {"ok": True},
    )
    base_url, _, _ = wizard_server
    # Save URL+token first so the state file has values
    _post(f"{base_url}/save", {
        "url": "homeassistant.local",
        "token": "good-token",
        "agent_id": "",
    })
    body = urllib.parse.urlencode({}).encode()
    req = urllib.request.Request(f"{base_url}/ready", data=body, method="POST")
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    assert data == {"ok": True}


def test_ready_endpoint_returns_no_when_unconfigured(wizard_server):
    """When URL+token aren't saved, /ready short-circuits to ok=false
    without trying to probe a missing HA. No mock needed — the real
    ready_sync sees empty URL+token and returns ok=false."""
    base_url, _, _ = wizard_server
    body = urllib.parse.urlencode({}).encode()
    req = urllib.request.Request(f"{base_url}/ready", data=body, method="POST")
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    assert data == {"ok": False}


def test_state_connected_html_masks_token(wizard_server):
    """The connected-state UI must NEVER render the full token into
    the page body. mask_secret() shows prefix…suffix only for the
    operator's display; the "📋 Copy with HA credentials" button
    fetches the live token lazily via POST /credentials-for-copy
    (see do_POST handler for the rationale). Regressing this means a
    browser extension / screenshot / save-page-as can exfiltrate the
    token without the user doing anything; tight invariant."""
    base_url, _, _ = wizard_server
    _post(f"{base_url}/save", {
        "url": "homeassistant.local",
        "token": "eyJ0eXAi-very-secret-token-content-here-xyz",
        "agent_id": "",
    })
    status, body = _get(f"{base_url}/")
    assert status == 200
    # Connected-state markers present
    assert "Connected" in body or "Disconnect" in body
    # Full token not in body
    assert "eyJ0eXAi-very-secret-token-content-here-xyz" not in body
    # Masked form is
    assert "eyJ0" in body  # prefix shown
    assert "…" in body
    # Defense-in-depth: the old leaky JS constants must not reappear.
    # If a future contributor inlines the token "for convenience,"
    # this regresses immediately.
    assert "VOICE_HA_TOKEN" not in body
    assert "VOICE_HA_URL" not in body


def test_credentials_for_copy_returns_url_and_token(wizard_server):
    """Happy path: POST with valid CSRF returns the live URL+token as
    JSON. This is what the "Copy with HA credentials" button calls."""
    import http.cookiejar
    import json as _json
    from ._web_test_helpers import CSRF_COOKIE_NAME

    base_url, _, _ = wizard_server
    _post(f"{base_url}/save", {
        "url": "homeassistant.local",
        "token": "eyJ0eXAi-secret-xyz",
        "agent_id": "",
    })
    # Mint the CSRF cookie via a GET, then POST with X-CSRF-Token header.
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPCookieProcessor(jar),
    )
    opener.open(f"{base_url}/").read()
    csrf_token = next(
        (c.value for c in jar if c.name == CSRF_COOKIE_NAME), ""
    )
    assert csrf_token, "wizard didn't mint a CSRF cookie on GET /"

    req = urllib.request.Request(
        f"{base_url}/credentials-for-copy",
        data=b"", method="POST",
        headers={"X-CSRF-Token": csrf_token},
    )
    with opener.open(req) as r:
        assert r.status == 200
        payload = _json.loads(r.read().decode())
    assert payload["url"] == "http://homeassistant.local:8123"  # _normalize_url
    assert payload["token"] == "eyJ0eXAi-secret-xyz"


def test_credentials_for_copy_rejects_missing_csrf(wizard_server):
    """Without the CSRF header (or with the wrong one), the endpoint
    returns 403. Defense against a malicious same-origin POST that
    can't read our CSRF cookie."""
    base_url, _, _ = wizard_server
    _post(f"{base_url}/save", {
        "url": "homeassistant.local",
        "token": "eyJ0eXAi-secret-xyz",
        "agent_id": "",
    })
    req = urllib.request.Request(
        f"{base_url}/credentials-for-copy",
        data=b"", method="POST",
    )
    try:
        urllib.request.urlopen(req)  # noqa: S310 — test only
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_credentials_for_copy_returns_400_when_no_state(wizard_server):
    """Defensive: if somehow the endpoint is hit before /save (state
    file missing), return 400 rather than empty strings. Shouldn't
    happen in practice because the page that calls this only renders
    when both URL+token are set."""
    import http.cookiejar
    from ._web_test_helpers import CSRF_COOKIE_NAME

    base_url, _, _ = wizard_server
    # Note: no /save. State file is empty.
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPCookieProcessor(jar),
    )
    opener.open(f"{base_url}/").read()
    csrf_token = next(
        (c.value for c in jar if c.name == CSRF_COOKIE_NAME), ""
    )
    req = urllib.request.Request(
        f"{base_url}/credentials-for-copy",
        data=b"", method="POST",
        headers={"X-CSRF-Token": csrf_token},
    )
    try:
        opener.open(req)
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400


# ---- helper ---------------------------------------------------------------

def _read_env(path: str) -> dict:
    """Parse the test's env file the same way _common.read_env_file would."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out
