# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static guardrails for web wizard UI/security conventions.

These are intentionally narrow tripwires for patterns that have already
caused maintenance or safety debt. They do not try to lint all HTML/JS;
they keep future wizard changes aligned with the shared primitives in
jasper.web._common.
"""
from __future__ import annotations

import ast
import functools
import http
import json
import re
import textwrap
import urllib.parse
from contextlib import nullcontext
from email.message import Message
from http.server import BaseHTTPRequestHandler
from io import BytesIO
from pathlib import Path

import pytest

from jasper.web import (
    airplay_setup,
    bluetooth_setup,
    chat_setup,
    correction_setup,
    google_setup,
    home_assistant_setup,
    rooms_setup,
    sources_setup,
    speaker_setup,
    spotify_setup,
    system_setup,
    tools_setup,
    voice_setup,
    wake_corpus_setup,
    wake_setup,
    weather_setup,
    wifi_setup,
)
from jasper.web._common import CSRF_COOKIE_NAME
from jasper.web.nav import NAV, hub_paths, render_hub


WEB_SETUP_FILES = (
    *Path("jasper/web").glob("*_setup.py"),
    *Path("jasper/web").glob("*_page.py"),
)
WEB_PY_FILES = tuple(sorted(Path("jasper/web").glob("*.py")))

_SHARED_JSON_OBJECT_READERS = {
    "bluetooth_setup.py": ("_read_json", "max_bytes=1_000_000"),
    "chat_setup.py": ("_read_json", "max_bytes=MAX_JSON_BYTES"),
    "wifi_setup.py": ("_read_json", "max_bytes=_JSON_BODY_LIMIT"),
    "sources_setup.py": ("_read_json", "max_bytes=_JSON_BODY_LIMIT"),
    "tools_setup.py": ("_read_json", "max_bytes=_JSON_BODY_LIMIT"),
    "wake_corpus_setup.py": ("_read_json", "max_bytes=_JSON_BODY_LIMIT"),
}


def _matches(pattern: str) -> list[str]:
    rx = re.compile(pattern)
    hits = []
    for path in WEB_SETUP_FILES:
        text = path.read_text()
        if rx.search(text):
            hits.append(str(path))
    return hits


def test_local_json_responses_use_the_shared_response_helper():
    offenders = set()
    for path in WEB_PY_FILES:
        if path.name == "_common.py":
            continue
        tree = ast.parse(path.read_text())
        content_type_senders = set()
        for function in (
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ):
            for call in (
                node for node in ast.walk(function) if isinstance(node, ast.Call)
            ):
                if (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "send_header"
                    and len(call.args) >= 2
                    and isinstance(call.args[0], ast.Constant)
                    and call.args[0].value == "Content-Type"
                ):
                    content_type_senders.add(function.name)
                    value = call.args[1]
                    if (
                        isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and value.value.startswith("application/json")
                    ):
                        offenders.add(path.name)
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr not in content_type_senders:
                continue
            if any(
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and arg.value.startswith("application/json")
                for arg in call.args
            ):
                offenders.add(path.name)
    assert not offenders


def test_migrated_local_object_responses_use_object_helper_not_byte_helper():
    for filename in (
        "bluetooth_setup.py",
        "home_assistant_setup.py",
        "rooms_setup.py",
        "sources_setup.py",
        "spotify_setup.py",
        "wake_corpus_setup.py",
        "wifi_setup.py",
    ):
        source = (Path("jasper/web") / filename).read_text()
        assert "send_json_response(" in source
        assert "send_proxy_json(" not in source


def test_migrated_json_object_readers_use_shared_helper_and_local_caps():
    for filename, (function_name, cap_call) in _SHARED_JSON_OBJECT_READERS.items():
        path = Path("jasper/web") / filename
        source = path.read_text(encoding="utf-8")
        functions = [
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        ]
        assert len(functions) == 1, (
            f"expected one {function_name} adapter in {path}"
        )
        adapter = ast.get_source_segment(source, functions[0]) or ""
        assert "read_json_object" in adapter
        assert cap_call in adapter
        assert ".headers" not in adapter
        assert "json.loads" not in adapter


# --- Mutating-request chokepoint: every wizard POST/DELETE handler funnels
# through the shared CSRF seam, and route-checks unknown paths FIRST.
#
# Wizard convention: every state-changing handler calls
# guard_mutating_request(), and "Route-check unknown POST paths before
# guard_mutating_request() so bogus paths return 404 without revealing CSRF
# state" (the convention block at the top of jasper/web/_common.py says the
# same). First run of the ordering guard caught wake_corpus_setup.py checking
# CSRF before routing in both do_POST and do_DELETE — bogus paths 403'd.

# wake_corpus_setup predates the shared double-submit seam and runs a
# reviewed bespoke scheme (server-held token + X-CSRF-Token header compare
# in _check_csrf). It is the only sanctioned exception to the
# guard_mutating_request chokepoint; do not grow this set. It is NOT
# exempt from the Host/Origin allowlist axis guard_mutating_request also
# applies — _check_csrf must call guard_mutating_host first, asserted
# below.
_BESPOKE_CSRF_WIZARDS = {"wake_corpus_setup.py"}
_CSRF_GUARD_CALL_RE = re.compile(
    r"\bguard_mutating_request\s*\(|\b_check_csrf\s*\(|\bguard_mutating_host\s*\("
)


def _call_target_name(call: ast.Call) -> str | None:
    """Resolve a Call's target name, bare (`f(...)`) or attribute
    (`mod.f(...)`) — `guard_mutating_host` and `secrets.compare_digest`
    are called in each shape."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _mutating_handlers():
    """Yield (path, func_name, source_segment) for every do_POST/do_DELETE
    defined under jasper/web (AST-walked, so docstring examples like the
    convention block in _common.py don't count)."""
    for path in WEB_PY_FILES:
        text = path.read_text()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.FunctionDef) and node.name in (
                "do_POST", "do_DELETE",
            ):
                yield path, node.name, ast.get_source_segment(text, node)


class _WizardRequest:
    """Drive a real wizard Handler instance without opening a socket."""

    def __init__(
        self,
        handler_cls,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        h = handler_cls.__new__(handler_cls)
        h.path = path
        h.headers = Message()
        h.headers["Content-Length"] = str(len(body))
        for key, value in (headers or {}).items():
            h.headers[key] = value
        h.rfile = BytesIO(body)
        h.wfile = BytesIO()
        h.client_address = ("127.0.0.1", 0)

        self.status: int | None = None
        self.sent_headers: list[tuple[str, str]] = []
        self.wfile = h.wfile
        self.rfile = h.rfile

        h.send_response = self._record_status
        h.send_response_only = self._record_status
        h.send_header = lambda name, value: self.sent_headers.append((name, value))
        h.end_headers = lambda: None
        h.send_error = functools.partial(BaseHTTPRequestHandler.send_error, h)
        h.address_string = lambda: "127.0.0.1"
        h.log_message = lambda *a, **k: None
        self._handler = h

    def _record_status(self, status, *args, **kwargs):  # noqa: ANN001
        self.status = int(status)

    def do_GET(self):
        self._handler.command = "GET"
        self._handler.do_GET()

    def do_POST(self):
        self._handler.command = "POST"
        self._handler.do_POST()


def test_wizard_get_rejects_dns_rebinding_host():
    for handler_cls in (wifi_setup._make_handler(), system_setup._make_handler()):
        req = _WizardRequest(handler_cls, "/", headers={"Host": "evil.example"})
        req.do_GET()
        assert req.status == int(http.HTTPStatus.FORBIDDEN)
        assert b"host_not_allowed" in req.wfile.getvalue()


def test_wizard_get_rejects_cross_site_fetch_metadata():
    req = _WizardRequest(
        wifi_setup._make_handler(),
        "/",
        headers={
            "Host": "jts.local",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "cors",
        },
    )
    req.do_GET()
    assert req.status == int(http.HTTPStatus.FORBIDDEN)
    assert b"cross_site_request" in req.wfile.getvalue()


def test_wizard_get_unknown_route_404s_before_read_guard():
    for handler_cls in (wifi_setup._make_handler(), system_setup._make_handler()):
        req = _WizardRequest(
            handler_cls,
            "/not-a-route",
            headers={"Host": "evil.example"},
        )
        req.do_GET()
        assert req.status == int(http.HTTPStatus.NOT_FOUND)


def test_wizard_get_allows_normal_management_host():
    for handler_cls in (wifi_setup._make_handler(), system_setup._make_handler()):
        req = _WizardRequest(handler_cls, "/", headers={"Host": "jts.local"})
        req.do_GET()
        assert req.status == int(http.HTTPStatus.OK)


def test_wizard_get_allows_cross_site_top_level_navigation():
    req = _WizardRequest(
        system_setup._make_handler(),
        "/",
        headers={
            "Host": "jts.local",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        },
    )
    req.do_GET()
    assert req.status == int(http.HTTPStatus.OK)


def test_state_changing_get_can_reject_cross_site_top_level_navigation():
    req = _WizardRequest(
        home_assistant_setup._make_handler({"state_path": "/tmp/jts-test-ha.env"}),
        "/reset",
        headers={
            "Host": "jts.local",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        },
    )
    req.do_GET()
    assert req.status == int(http.HTTPStatus.FORBIDDEN)
    assert b"cross_site_request" in req.wfile.getvalue()


def test_wifi_polling_state_get_still_works_with_normal_host(monkeypatch):
    monkeypatch.setattr(
        wifi_setup,
        "gather_state",
        lambda: {
            "adapterPresent": True,
            "radioOn": True,
            "hasEthernet": False,
            "lockoutRisk": "low",
            "current": None,
            "saved": [],
        },
    )
    req = _WizardRequest(
        wifi_setup._make_handler(),
        "/state",
        headers={"Host": "jts.local"},
    )
    req.do_GET()
    assert req.status == int(http.HTTPStatus.OK)
    assert json.loads(req.wfile.getvalue().decode())["lockoutRisk"] == "low"


def _spotify_handler_cls():
    return spotify_setup._make_handler({
        "client_id": "",
        "mode": "bounce",
        "registry_path": "/tmp/jts-test-spotify-accounts.json",
        "bounce_redirect_uri": (
            "https://jaspercurry.github.io/spotify-oauth-callback/?host=jts.local"
        ),
        "manual_redirect_uri": "http://127.0.0.1:8888/callback",
    })


def _google_handler_cls():
    return google_setup._make_handler({
        "creds_path": "/tmp/jts-test-google-credentials.env",
        "redirect_uri": (
            "https://jaspercurry.github.io/google-oauth-callback/?host=jts.local"
        ),
        "registry_path": "/tmp/jts-test-google-accounts.json",
    })


def test_oauth_callbacks_allow_cross_site_top_level_navigation():
    headers = {
        "Host": "jts.local",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    cases = (
        (_spotify_handler_cls(), "/oauth-callback"),
        (_google_handler_cls(), "/callback"),
    )
    for handler_cls, path in cases:
        req = _WizardRequest(handler_cls, path, headers=headers)
        req.do_GET()
        assert req.status == int(http.HTTPStatus.SEE_OTHER)


def test_oauth_redirect_follow_index_allows_cross_site_top_level_navigation():
    headers = {
        "Host": "jts.local",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    cases = (
        (_spotify_handler_cls(), "/"),
        (_google_handler_cls(), "/"),
    )
    for handler_cls, path in cases:
        req = _WizardRequest(handler_cls, path, headers=headers)
        req.do_GET()
        assert req.status == int(http.HTTPStatus.OK)


def test_oauth_callbacks_still_reject_cross_site_fetch_reads():
    headers = {
        "Host": "jts.local",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
    }
    cases = (
        (_spotify_handler_cls(), "/oauth-callback"),
        (_google_handler_cls(), "/callback"),
    )
    for handler_cls, path in cases:
        req = _WizardRequest(handler_cls, path, headers=headers)
        req.do_GET()
        assert req.status == int(http.HTTPStatus.FORBIDDEN)
        assert b"cross_site_request" in req.wfile.getvalue()


# --- Route tables, pinned at the request surface --------------------------
#
# Every converged wizard dispatches the same five steps: normalise the path,
# look it up in a table, 404 if absent, guard, call. These pins drive real
# handler instances instead of reading a dispatcher's source, so a wizard is
# free to hold its table in a closure, on the class, or at module level.

# The recorder's bespoke scheme compares a server-held token, so the pins
# hand it back the same one _make_handler_class was built with.
_WAKE_CORPUS_TOKEN = "wake-corpus-test-token"
# Syntactically valid double-submit token (base64url, 32..128 chars).
_VALID_CSRF_TOKEN = "A" * 43

_TABLED_WIZARD_FACTORIES = {
    "airplay_setup": lambda: airplay_setup._make_handler(
        {"state_path": "/tmp/jts-test-airplay.env"},
    ),
    "bluetooth_setup": lambda: bluetooth_setup._make_handler(),
    "chat_setup": chat_setup._make_handler,
    "correction_setup": lambda: correction_setup._make_handler_class(
        hostname="jts.local", idle_hold=nullcontext,
    ),
    "rooms_setup": rooms_setup._make_handler,
    "sources_setup": sources_setup._make_handler,
    "speaker_setup": lambda: speaker_setup._make_handler(
        {"state_path": "/tmp/jts-test-speaker.env"},
    ),
    "spotify_setup": _spotify_handler_cls,
    "system_setup": system_setup._make_handler,
    "tools_setup": lambda: tools_setup._make_handler({
        "catalog_path": "/tmp/jts-test-tools-catalog.json",
        "state_path": "/tmp/jts-test-tool-state.env",
        "prompt_overrides_path": "/tmp/jts-test-tool-prompt-overrides.json",
    }),
    "wake_corpus_setup": lambda: wake_corpus_setup._make_handler_class(
        object(), _WAKE_CORPUS_TOKEN,
    ),
    "home_assistant_setup": lambda: home_assistant_setup._make_handler(
        {"state_path": "/tmp/jts-test-ha.env"},
    ),
    "wake_setup": lambda: wake_setup._make_handler(
        {
            "state_path": "/tmp/jts-test-wake.env",
            "control_base": "http://127.0.0.1:8780",
        },
    ),
    "wifi_setup": wifi_setup._make_handler,
    "voice_setup": lambda: voice_setup._make_handler({
        "state_path": "/tmp/jts-test-voice-provider.env",
        "keys_path": "/tmp/jts-test-voice-keys.env",
        "discovery_cache_path": "/tmp/jts-test-voice-models.json",
        "discovery_http_client": None,
        "pricing_path": "/tmp/jts-test-voice-pricing.json",
        "assistant_loudness_profile_path": "/tmp/jts-test-voice-loudness.json",
        "loudness_seed_fn": lambda *a, **k: None,
    }),
    "weather_setup": lambda: weather_setup._make_handler({
        "state_path": "/tmp/jts-test-weather.env",
        "transit_path": "/tmp/jts-test-weather-transit.env",
    }),
}

# Wizards whose CSRF token rides in a header, so the guard runs before any
# body read. The rest are form wizards, which cannot guard before reading
# the body the token is in. Shrinks as a wizard moves to header CSRF.
_HEADER_CSRF_WIZARDS = frozenset({
    "bluetooth_setup",
    "chat_setup",
    "correction_setup",
    "rooms_setup",
    "sources_setup",
    "system_setup",
    "tools_setup",
    "wifi_setup",
})


def _route_table_paths(source_path: Path) -> dict[str, list[str]]:
    """The paths a wizard's `_GET_ROUTES` / `_POST_ROUTES` dict literals
    declare, wherever in the module they are assigned — AST, not import, so
    a closure-local table counts the same as a module-level one."""
    tables: dict[str, list[str]] = {"_GET_ROUTES": [], "_POST_ROUTES": []}
    for node in ast.walk(ast.parse(source_path.read_text())):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in tables:
                tables[target.id].extend(
                    key.value for key in node.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
    return tables


def _resolve_samples(dispatch_fn) -> dict:
    """`{sample path: route}` for each prefix family a dispatcher's `resolve=`
    hook answers. `_common.resolve_samples` stamps the mapping on the hook,
    and the hook is reachable from the dispatcher the same two ways a route
    table is: a closure cell, or a module global it names."""
    if dispatch_fn is None:
        return {}
    reachable = [cell.cell_contents for cell in dispatch_fn.__closure__ or ()]
    reachable += [
        dispatch_fn.__globals__[name]
        for name in dispatch_fn.__code__.co_names
        if name in dispatch_fn.__globals__
    ]
    for value in reachable:
        samples = getattr(value, "resolve_samples", None)
        if isinstance(samples, dict):
            return samples
    return {}


def _sample_paths(handler_cls, dispatcher: str) -> list[str]:
    return list(_resolve_samples(getattr(handler_cls, dispatcher, None)))


def _tabled_wizards():
    """(module name, handler class, GET paths, POST paths) per tabled wizard.

    Paths are the dict-literal keys plus one sample per prefix family the
    dispatcher's `resolve=` hook declares, so `/layer/<name>` is covered by
    the same generic pins an exact key is."""
    out = []
    for source_path in sorted(WEB_SETUP_FILES):
        tables = _route_table_paths(source_path)
        if not (tables["_GET_ROUTES"] or tables["_POST_ROUTES"]):
            continue
        module_name = source_path.stem
        factory = _TABLED_WIZARD_FACTORIES.get(module_name)
        assert factory is not None, (
            f"{source_path} grew a route table with no entry in "
            "_TABLED_WIZARD_FACTORIES — add one so its routes are covered"
        )
        handler_cls = factory()
        out.append((
            module_name,
            handler_cls,
            tables["_GET_ROUTES"] + _sample_paths(handler_cls, "do_GET"),
            tables["_POST_ROUTES"] + _sample_paths(handler_cls, "do_POST"),
        ))
    return out


TABLED_WIZARDS = _tabled_wizards()
TABLED_GET_ROUTES = [
    (name, cls, path) for name, cls, gets, _ in TABLED_WIZARDS for path in gets
]
# POST routes guarded by the READ guard rather than by CSRF: read-only
# probes that change no state. A missing CSRF token is not what rejects
# them, so they are pinned against a cross-site read instead.
_READ_GUARDED_POST_ROUTES = frozenset({
    ("home_assistant_setup", "/discover"),
    ("home_assistant_setup", "/ready"),
    ("home_assistant_setup", "/verify"),
})
TABLED_POST_ROUTES = [
    (name, cls, path) for name, cls, _, posts in TABLED_WIZARDS for path in posts
    if (name, path) not in _READ_GUARDED_POST_ROUTES
]
READ_GUARDED_POST_ROUTES = [
    (name, cls, path) for name, cls, _, posts in TABLED_WIZARDS for path in posts
    if (name, path) in _READ_GUARDED_POST_ROUTES
]
TABLED_POST_WIZARDS = [
    (name, cls) for name, cls, _, posts in TABLED_WIZARDS if posts
]


# wifi_setup's `_read_json` coerces a malformed body to {} on purpose, so its
# routes run their bodies instead of the decorator's 400 — pinned in
# tests/test_web_wifi_setup.py, excluded here.
_COERCES_MALFORMED_BODY = frozenset({"wifi_setup"})


def _post_route_table(handler_cls) -> dict:
    """The wizard's live POST table plus its prefix-family samples — a
    closure cell on `do_POST` when the table is closure-local (it captures
    per-server cfg), else a module global. Same reach the header-CSRF pins
    use to drive the real callables."""
    fn = handler_cls.do_POST
    freevars = fn.__code__.co_freevars
    if "_POST_ROUTES" in freevars:
        table = fn.__closure__[freevars.index("_POST_ROUTES")].cell_contents
    else:
        table = fn.__globals__["_POST_ROUTES"]
    return {**table, **_resolve_samples(fn)}


# `csrf_mode` per POST route — the marker `form_guarded` / `header_guarded` /
# `read_guarded` stamp on the wrapper they return, so a wizard whose guard
# varies per route declares its axis there rather than in a list kept here.
_POST_ROUTE_CSRF_MODES = {
    (name, path): mode
    for name, cls, _, posts in TABLED_WIZARDS if posts
    for path, fn in _post_route_table(cls).items()
    if (mode := getattr(fn, "csrf_mode", None)) is not None
}


# POST routes whose body parses through `_common.json_body` — the decorator
# marks its wrapper, so this tracks the routes themselves rather than a
# hand-kept wizard list.
_JSON_BODY_POST_ROUTES = [
    (name, cls, path)
    for name, cls, _, posts in TABLED_WIZARDS
    if posts and name not in _COERCES_MALFORMED_BODY
    for path in posts
    if getattr(_post_route_table(cls).get(path), "reads_json_body", False)
]


def _csrf_headers(module_name: str) -> dict[str, str]:
    if f"{module_name}.py" in _BESPOKE_CSRF_WIZARDS:
        return {"X-CSRF-Token": _WAKE_CORPUS_TOKEN}
    return {
        "Cookie": f"{CSRF_COOKIE_NAME}={_VALID_CSRF_TOKEN}",
        "X-CSRF-Token": _VALID_CSRF_TOKEN,
    }


@pytest.mark.parametrize(
    ("module_name", "handler_cls", "path"),
    TABLED_POST_ROUTES,
    ids=[f"{name}{path}" for name, _, path in TABLED_POST_ROUTES],
)
def test_tabled_post_route_without_a_csrf_token_is_forbidden(
    module_name, handler_cls, path,
):
    req = _WizardRequest(
        handler_cls, path, headers={"Host": "jts.local"}, body=b'{"on": true}',
    )
    req.do_POST()
    assert req.status == int(http.HTTPStatus.FORBIDDEN)
    if (
        module_name in _HEADER_CSRF_WIZARDS
        or _POST_ROUTE_CSRF_MODES.get((module_name, path)) == "header"
    ):
        # The guard runs before any body read, so a rejected POST leaves the
        # request body unconsumed and cannot have mutated anything. Header
        # CSRF is a per-module fact for the wizards that guard in their
        # dispatcher and a per-route one for those that guard per body.
        assert req.rfile.tell() == 0


@pytest.mark.parametrize(
    ("module_name", "handler_cls"),
    TABLED_POST_WIZARDS,
    ids=[name for name, _ in TABLED_POST_WIZARDS],
)
def test_tabled_wizard_unknown_post_path_404s_with_or_without_a_token(
    module_name, handler_cls,
):
    for token_headers in ({}, _csrf_headers(module_name)):
        req = _WizardRequest(
            handler_cls,
            "/not-a-route",
            headers={"Host": "jts.local", **token_headers},
            body=b'{"on": true}',
        )
        req.do_POST()
        assert req.status == int(http.HTTPStatus.NOT_FOUND)
        # The seam 404s through the stdlib error page, so the miss carries a
        # body and a content type rather than a bare status line.
        assert any(
            name == "Content-Type" and value.startswith("text/html")
            for name, value in req.sent_headers
        )


@pytest.mark.parametrize(
    ("module_name", "handler_cls", "path"),
    READ_GUARDED_POST_ROUTES,
    ids=[f"{name}{path}" for name, _, path in READ_GUARDED_POST_ROUTES],
)
def test_read_guarded_post_route_rejects_cross_site_reads(
    module_name, handler_cls, path,
):
    req = _WizardRequest(
        handler_cls,
        path,
        headers={
            "Host": "jts.local",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "cors",
        },
    )
    req.do_POST()
    assert req.status == int(http.HTTPStatus.FORBIDDEN)
    assert b"cross_site_request" in req.wfile.getvalue()


@pytest.mark.parametrize(
    ("module_name", "handler_cls", "path"),
    TABLED_GET_ROUTES,
    ids=[f"{name}{path}" for name, _, path in TABLED_GET_ROUTES],
)
def test_tabled_get_route_rejects_cross_site_reads(module_name, handler_cls, path):
    req = _WizardRequest(
        handler_cls,
        path,
        headers={
            "Host": "jts.local",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "cors",
        },
    )
    req.do_GET()
    assert req.status == int(http.HTTPStatus.FORBIDDEN)
    assert b"cross_site_request" in req.wfile.getvalue()


@pytest.mark.parametrize(
    ("module_name", "handler_cls", "path"),
    _JSON_BODY_POST_ROUTES,
    ids=[f"{name}{path}" for name, _, path in _JSON_BODY_POST_ROUTES],
)
def test_a_malformed_json_body_never_reaches_a_route_body(
    module_name, handler_cls, path,
):
    """`json_body` parses before it dispatches: a token-bearing POST whose
    body is not a JSON object is answered 400 by the wizard's `_read_json`,
    so the route body never runs."""
    req = _WizardRequest(
        handler_cls,
        path,
        headers={"Host": "jts.local", **_csrf_headers(module_name)},
        body=b"{not json",
    )
    req.do_POST()
    assert req.status == int(http.HTTPStatus.BAD_REQUEST)


# --- Wizards not yet on a route table ------------------------------------
#
# The tabled wizards are pinned at the request surface above. Until the rest
# join them these two source-level tripwires are the only thing holding the
# read guard and the guard-before-work ordering in their hand-rolled
# dispatchers.
#
# Removal condition: delete once every wizard is in
# _TABLED_WIZARD_FACTORIES (routes part A).

UNTABLED_WIZARD_FILES = [
    path for path in sorted(WEB_SETUP_FILES)
    if path.stem not in _TABLED_WIZARD_FACTORIES
]

# The first thing a do_POST does with the request that is not the CSRF guard's
# own input. `read_form` is deliberately absent: a form wizard's token rides in
# the body, so reading it is how the guard gets called at all.
_POST_WORK_MARKERS = (
    "self._read_json(",
    "self._handle_",
)
_POST_GUARD_MARKERS = ("guard_mutating_request(", "self._check_csrf(")


def _dispatcher_source(path: Path, name: str) -> str | None:
    source = path.read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node)
    return None


@pytest.mark.parametrize(
    "path", UNTABLED_WIZARD_FILES, ids=[p.stem for p in UNTABLED_WIZARD_FILES],
)
def test_untabled_wizard_dispatchers_guard_reads_and_guard_before_work(path):
    get_source = _dispatcher_source(path, "do_GET")
    if get_source is not None:
        assert "guard_read_request" in get_source, (
            f"{path}::do_GET never calls guard_read_request() — the shared "
            "Host + Fetch Metadata read chokepoint in jasper/web/_common.py"
        )
    post_source = _dispatcher_source(path, "do_POST")
    if post_source is None:
        return
    guards = [post_source.index(m) for m in _POST_GUARD_MARKERS if m in post_source]
    work = [post_source.index(m) for m in _POST_WORK_MARKERS if m in post_source]
    if not work:
        return
    assert guards and min(guards) < min(work), (
        f"{path}::do_POST reads the body or dispatches a route before the "
        "CSRF guard runs"
    )


def test_bespoke_csrf_scheme_guards_the_host_before_the_token_compare():
    """The sanctioned bespoke scheme is exempt from the shared
    double-submit chokepoint, not from the Host/Origin allowlist axis
    `guard_mutating_request` also applies."""
    for file_name in _BESPOKE_CSRF_WIZARDS:
        path = next(p for p in WEB_PY_FILES if p.name == file_name)
        source = path.read_text()
        check_csrf_defs = [
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "_check_csrf"
        ]
        assert len(check_csrf_defs) == 1, f"expected one _check_csrf in {path}"
        csrf_calls = [
            node for node in ast.walk(check_csrf_defs[0])
            if isinstance(node, ast.Call)
        ]
        # AST-walk for a real Call node, not a regex over the source
        # text — a docstring/comment reading "guard_mutating_host(handler)"
        # must not satisfy this.
        guard_calls = [
            c for c in csrf_calls if _call_target_name(c) == "guard_mutating_host"
        ]
        assert guard_calls, (
            f"{path}::_check_csrf must call guard_mutating_host() first — "
            "the shared Host/Origin allowlist axis is not part of the "
            "bespoke-CSRF-scheme exception"
        )
        compare_calls = [
            c for c in csrf_calls if _call_target_name(c) == "compare_digest"
        ]
        assert compare_calls, (
            f"{path}::_check_csrf must compare the token with "
            "secrets.compare_digest()"
        )
        # Ordering invariant guard_mutating_request's docstring documents
        # (_common.py): the host/Origin guard runs before the token
        # compare. AST line positions, not string indexes.
        assert (
            min(c.lineno for c in guard_calls)
            < min(c.lineno for c in compare_calls)
        ), (
            f"{path}::_check_csrf must call guard_mutating_host() before "
            "the compare_digest() token compare"
        )


# The chokepoint reach a do_POST may have: the guard called in the
# dispatcher, the shared `dispatch_post` seam that calls it there, or
# wake_corpus_setup's sanctioned bespoke scheme (pinned above).
_CHOKEPOINT_CALLS = ("guard_mutating_request", "dispatch_post", "_check_csrf")


def test_every_wizard_mutating_handler_uses_the_csrf_chokepoint():
    """A form wizard's guard lives in the route body — it must read the
    body to find the token — so it is pinned behaviourally above instead.
    Every other do_POST still reaches the chokepoint itself."""
    handlers = list(_mutating_handlers())
    assert handlers, "expected wizard do_POST handlers to scan"
    body_guarded = {
        f"{name}.py" for name, _, _, _ in TABLED_WIZARDS
        if name not in _HEADER_CSRF_WIZARDS
    }
    offenders = []
    for path, name, seg in handlers:
        if path.name in body_guarded:
            continue
        if path.name == "__main__.py":
            # The colocated-server router only delegates to the per-wizard
            # handlers (which each guard themselves) — assert it stays a
            # pure delegator rather than growing unguarded routes.
            assert "_delegate" in seg, (
                f"{path}::{name} no longer delegates — it must call "
                "guard_mutating_request() itself"
            )
            continue
        # AST Call, not a substring — a comment naming the guard must not
        # satisfy the chokepoint.
        handler_fn = ast.parse(textwrap.dedent(seg)).body[0]
        if not any(
            isinstance(node, ast.Call)
            and _call_target_name(node) in _CHOKEPOINT_CALLS
            for node in ast.walk(handler_fn)
        ):
            offenders.append(f"{path}::{name}")
    assert offenders == [], (
        "wizard mutating handlers that never reach guard_mutating_request() "
        "(the shared Host/Origin + CSRF chokepoint in jasper/web/_common.py):\n"
        + "\n".join(offenders)
    )


def test_mutating_handlers_route_check_before_csrf_guard():
    """The first conditional in a hand-rolled do_POST/do_DELETE must be
    routing, never the CSRF guard: 'Route-check unknown POST paths before
    guard_mutating_request() so bogus paths return 404 without revealing
    CSRF state' (AGENTS.md / jasper/web/_common.py). In every compliant
    handler the first `if` tests the request path or a route-table lookup
    of it; a handler whose first branch is the guard 403s on bogus paths
    instead. A dispatcher on the shared `dispatch_post` seam has no branch
    of its own — the seam looks the route up first, pinned behaviourally by
    test_tabled_wizard_unknown_post_path_404s_with_or_without_a_token."""
    branch_re = re.compile(r"^\s*(?:if|elif)\b")
    offenders = []
    for path, name, seg in _mutating_handlers():
        if path.name == "__main__.py":
            continue  # pure delegator, asserted above
        if "dispatch_post(" in seg:
            continue  # on the seam; ordering is the seam's, pinned there
        for line in seg.splitlines():
            if not branch_re.match(line):
                continue
            if _CSRF_GUARD_CALL_RE.search(line):
                offenders.append(
                    f"{path}::{name} guards CSRF before route-checking: "
                    + line.strip()
                )
            break  # only the first branch matters
    assert offenders == [], (
        "route-check unknown paths (404) BEFORE the CSRF guard so bogus "
        "paths don't reveal CSRF state:\n" + "\n".join(offenders)
    )


def test_wizards_do_not_reintroduce_div_switches():
    assert _matches(r"class=[\"']switch[\"']") == []


def test_wizards_do_not_reintroduce_json_posts_without_csrf_helper():
    assert _matches(
        r"headers:\s*\{\s*['\"]Content-Type['\"]\s*:\s*"
        r"['\"]application/json['\"]\s*\}",
    ) == []


def test_wizards_do_not_generate_inline_js_for_untrusted_metadata():
    risky_handlers = (
        "connectDevice",
        "startPair",
        "forget(",
        "openConnect",
        "openForget",
        "submitConnect",
        "submitForget",
        "dismissPanel",
        "dismissForget",
        "toggleRadio",
        "provision",
    )
    for handler in risky_handlers:
        assert _matches(r"onclick=[\"']" + re.escape(handler)) == []


def test_wizards_do_not_need_js_string_attribute_escaping_helper():
    assert _matches(r"function\s+jsArg\b") == []


# Wizard convention: "Do not put untrusted strings into
# generated inline JavaScript such as onclick=\"handler('...')\". Prefer
# escaped data-* attributes with a delegated click handler." The fixed
# risky-handler list above only catches names it knows about; these two
# tripwires catch the *shape* — an inline on<event>= attribute whose value
# interpolates a runtime value — wherever it next appears. (There is no
# HTML-attribute-safe way to embed an arbitrary string inside inline JS;
# every current page uses data-* + delegation, so the clean state is zero.)

# Python f-string interpolation into an inline handler attribute:
#   onclick="forget('{name}')"  /  onclick='forget("{name}")'
_PY_INLINE_HANDLER_INTERP_RE = re.compile(
    r"""\bon[a-z]+=(?:"[^"\n]*\{[^"\n]*"|'[^'\n]*\{[^'\n]*')"""
)


def test_wizard_python_does_not_interpolate_into_inline_handler_js():
    offenders = []
    for path in WEB_PY_FILES:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _PY_INLINE_HANDLER_INTERP_RE.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "interpolated value inside a generated inline on<event>= handler — "
        "use an escaped data-* attribute + a delegated listener instead:\n"
        + "\n".join(offenders)
    )


# Template-literal interpolation into an inline handler attribute built by
# an ES module:  el.innerHTML = `... onclick="forget('${name}')" ...`
_JS_INLINE_HANDLER_INTERP_RE = re.compile(
    r"""\bon[a-z]+=(?:\\?"[^"\n]*\$\{|\\?'[^'\n]*\$\{)"""
)


def test_static_modules_do_not_interpolate_into_inline_handler_js():
    assert WEB_MODULE_FILES, "expected web ES modules to scan"
    offenders = []
    for path in WEB_MODULE_FILES:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _is_comment_line(line):
                continue
            if _JS_INLINE_HANDLER_INTERP_RE.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "interpolated value inside a module-built inline on<event>= handler — "
        "use an escaped data-* attribute + a delegated listener instead:\n"
        + "\n".join(offenders)
    )


# Redesigned pages deliver their behaviour as static ES modules under
# deploy/assets/<page>/js/** — outside the *_setup.py scan above.
WEB_MODULE_FILES = tuple(Path("deploy/assets").glob("*/js/**/*.js"))


def test_static_modules_do_not_reintroduce_json_posts_without_csrf_helper():
    """The CSRF-helper rule follows the JS to its new home: JSON POSTs from a
    module go through jsonHeaders() (which attaches X-CSRF-Token), never a raw
    inline Content-Type header."""
    assert WEB_MODULE_FILES, "expected web ES modules to scan"
    rx = re.compile(
        r"headers:\s*\{\s*['\"]Content-Type['\"]\s*:\s*"
        r"['\"]application/json['\"]\s*\}",
    )
    offenders = [str(p) for p in WEB_MODULE_FILES if rx.search(p.read_text())]
    assert offenders == []


def test_sync_measurement_recorder_uses_worklet_without_mic_monitoring():
    src = Path("deploy/assets/sync/js/main.js").read_text()

    assert "/assets/shared/js/measurement-audio.js" in src
    assert "createMonoRecorder" in src
    assert "float32ToWavBlob" in src
    assert "getUserMedia" not in src
    assert "new AudioContext" not in src
    assert "AudioWorkletProcessor" not in src
    assert "AudioWorkletNode" not in src
    assert "createScriptProcessor" not in src
    assert ".destination" not in src


_SHARED_MEASUREMENT_AUDIO_MODULE = Path(
    "deploy/assets/shared/js/measurement-audio.js"
)


def test_shared_measurement_audio_module_owns_capture_primitives():
    src = _SHARED_MEASUREMENT_AUDIO_MODULE.read_text()

    for name in (
        "monoMicConstraints",
        "openMonoMic",
        "micCaptureSupport",
        "assertMicCaptureSupported",
        "createBandpassRmsMeter",
        "createMonoRecorder",
        "float32ToWavBlob",
        "closeAudioGraph",
    ):
        assert re.search(r"export\s+(?:async\s+)?function\s+" + name + r"\b", src)
    assert "navigator.mediaDevices.getUserMedia" in src
    assert "non_secure_context" in src
    assert "media_devices_unavailable" in src
    assert "Microphone capture needs HTTPS" in src
    assert "AudioWorkletProcessor" in src
    assert "createMediaStreamSource" in src
    assert "sourceNode.connect(workletNode)" in src
    assert "createScriptProcessor" not in src
    assert ".destination" not in src


# Native browser dialogs — confirm()/alert()/prompt() — are being retired
# across the UI in favour of the shared <dialog> helper exported from
# /assets/shared/js/dialog.js (jtsConfirm / jtsAlert). The browser can suppress
# the native popups ("prevent this page from creating more dialogs"), which
# silently defeated the speaker's restart/reboot guards. The canonical ES
# modules must not reintroduce them.
_NATIVE_DIALOG_RE = re.compile(r"(?<![\w.$])(?:window\.)?(?:confirm|alert|prompt)\s*\(")


def _is_comment_line(line: str) -> bool:
    """True for whole-line JS comments (// …, /* …, or a * continuation).

    The native-dialog scan skips these so the dialog helper's own docstrings
    (which necessarily *name* confirm()/alert()) don't read as offenders. Real
    calls live on code lines; the migrated modules call jtsConfirm/jtsAlert
    (capitalised), which the lowercase-only regex never matches."""
    stripped = line.lstrip()
    return stripped.startswith(("//", "*", "/*"))


def test_static_modules_do_not_use_native_browser_dialogs():
    assert WEB_MODULE_FILES, "expected web ES modules to scan"
    offenders = []
    for path in WEB_MODULE_FILES:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _is_comment_line(line):
                continue
            if _NATIVE_DIALOG_RE.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "native confirm()/alert()/prompt() in canonical ES modules — use "
        "jtsConfirm/jtsAlert from /assets/shared/js/dialog.js instead:\n"
        + "\n".join(offenders)
    )


# The same retirement applies to any Python-rendered wizard strings. Dialog
# interactions belong in static ES modules that import
# /assets/shared/js/dialog.js, not in inline JavaScript inside *_setup.py.

# A native call: the name not preceded by an identifier char or dot (excludes
# jtsConfirm, obj.alert, respond_prompt) and opening immediately on a string /
# template-literal argument (excludes prose like "the confirm() dialog" in
# docstrings/comments, which carries no quote after the paren).
_NATIVE_DIALOG_CALL_RE = re.compile(
    r"(?<![\w.$])(?:window\.)?(?:confirm|alert|prompt)\s*\(\s*['\"`]"
)


def test_wizards_do_not_use_native_browser_dialogs():
    offenders = []
    for path in WEB_SETUP_FILES:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _is_comment_line(line):
                continue
            if _NATIVE_DIALOG_CALL_RE.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "native confirm()/alert()/prompt() in a wizard — use jtsConfirm/jtsAlert "
        "from /assets/shared/js/dialog.js instead:\n" + "\n".join(offenders)
    )


def test_wizard_python_strings_do_not_inline_dialog_helper_calls():
    """Dialog helper calls live in static ES modules, not Python strings."""
    uses_helper = re.compile(r"\bjts(?:Confirm|Alert|ConfirmSubmit)\s*\(")
    offenders = []
    for path in WEB_SETUP_FILES:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _is_comment_line(line):
                continue
            if uses_helper.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "dialog helper calls in Python-rendered wizard strings — move the "
        "interaction to a static ES module that imports /assets/shared/js/dialog.js:\n"
        + "\n".join(offenders)
    )


# app.css's only hiding rule is the native ATTRIBUTE selector,
# `[hidden] { display: none !important; }` — there is no `.hidden` CLASS
# rule in the shared stylesheet. A page that hides an element with
# `class="... hidden ..."` or `classList.add('hidden')` instead of the
# `hidden` attribute renders the control fully visible; app.css never wires
# a class named "hidden" to anything. This shipped invisibly on the
# crossover page (hardware-confirmed 2026-07-16): the retired "Open phone
# capture" / "Stop measurement" controls stayed on screen on every step
# because crossover.css carries no local `.hidden` rule either.
_HIDDEN_CLASSLIST_RE = re.compile(
    r"classList\.(?:add|remove|toggle)\(\s*['\"]hidden['\"]"
)
_CLASS_ATTR_RE = re.compile(r"""class=(["'])(.*?)\1""")


def test_static_modules_hide_elements_with_the_attribute_not_a_class():
    assert WEB_MODULE_FILES, "expected web ES modules to scan"
    offenders = []
    for path in WEB_MODULE_FILES:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _HIDDEN_CLASSLIST_RE.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "classList('hidden') with no local `.hidden` CSS rule for this page "
        "— app.css only implements the native `hidden` attribute; use "
        "`el.hidden = true/false` instead:\n" + "\n".join(offenders)
    )


def test_wizard_markup_hides_elements_with_the_attribute_not_a_class():
    offenders = []
    for path in WEB_PY_FILES:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for _quote, class_value in _CLASS_ATTR_RE.findall(line):
                if "hidden" in class_value.split():
                    offenders.append(f"{path}:{lineno}: {line.strip()}")
                    break
    assert offenders == [], (
        'class="...hidden..." with no local `.hidden` CSS rule for this '
        "page — app.css only implements the native `hidden` attribute; "
        "render a bare `hidden` attribute instead:\n" + "\n".join(offenders)
    )


# The HTML-entity escaper (the five-char & < > " ' table) was copied across the
# wifi/bluetooth/sound-profile/correction modules under two names
# (escapeHtml / escapeText) before it was promoted to the shared module at
# /assets/shared/js/escape.js (same shared-by-promotion path as dialog.js /
# http.js). Pages now import escapeHtml (and the escapeAttr alias / cssIdSafe)
# from there. This test keeps the duplication from creeping back: no canonical
# module may declare its own escapeHtml/escapeText again — escape.js is the one
# home.
_SHARED_ESCAPE_MODULE = Path("deploy/assets/shared/js/escape.js")
_LOCAL_ESCAPER_DEF_RE = re.compile(r"function\s+(?:escapeHtml|escapeText)\b")


def test_shared_escape_module_exists_and_exports_the_escaper():
    """The drift test below is only meaningful once the shared home exists and
    exports the names pages import."""
    assert _SHARED_ESCAPE_MODULE.is_file(), (
        f"{_SHARED_ESCAPE_MODULE} (shared HTML escaper) is missing"
    )
    src = _SHARED_ESCAPE_MODULE.read_text()
    assert re.search(r"export\s+function\s+escapeHtml\b", src), (
        "escape.js must export escapeHtml"
    )
    # escapeAttr is an explicit alias; cssIdSafe rides along (wifi/bluetooth).
    assert "escapeAttr" in src, "escape.js must expose the escapeAttr alias"
    assert re.search(r"export\s+function\s+cssIdSafe\b", src), (
        "escape.js must export cssIdSafe"
    )


def test_modules_do_not_redefine_the_shared_html_escaper():
    """No deploy/assets module re-declares escapeHtml/escapeText now that the
    shared escape.js owns it — they import from /assets/shared/js/escape.js
    instead. escape.js itself is the canonical definition and is exempt."""
    assert WEB_MODULE_FILES, "expected web ES modules to scan"
    offenders = []
    for path in WEB_MODULE_FILES:
        if path.resolve() == _SHARED_ESCAPE_MODULE.resolve():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _LOCAL_ESCAPER_DEF_RE.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "these modules redefine the shared HTML escaper — import escapeHtml "
        "(or escapeAttr / the escapeText alias) from /assets/shared/js/escape.js "
        "instead:\n" + "\n".join(offenders)
    )


# The text-node DOM builder (h() / svg()) is the entire basis of the
# "untrusted strings never reach innerHTML" safety argument: string children
# become text nodes, so transcripts, provider names, device labels, etc. are
# escaped by the DOM. It was copy-pasted across the /assistant/chat/ and
# /system/ module graphs (and had already drifted — `catch (_)` vs `catch`,
# divergent comments) before it was promoted to the shared module at
# /assets/shared/js/dom.js (same shared-by-promotion path as dialog.js /
# escape.js / http.js). Pages now import
# h/svg from there. This test keeps the duplication from creeping back: no
# canonical module may re-declare its own h()/svg() builder again — dom.js is
# the one home for the XSS-safety primitive.
_SHARED_DOM_MODULE = Path("deploy/assets/shared/js/dom.js")
_LOCAL_DOM_BUILDER_DEF_RE = re.compile(r"function\s+(?:h|svg)\b")


def test_shared_dom_module_exists_and_exports_the_builder():
    """The drift test below is only meaningful once the shared home exists and
    exports the names pages import."""
    assert _SHARED_DOM_MODULE.is_file(), (
        f"{_SHARED_DOM_MODULE} (shared text-node DOM builder) is missing"
    )
    src = _SHARED_DOM_MODULE.read_text()
    assert re.search(r"export\s+function\s+h\b", src), (
        "dom.js must export h"
    )
    assert re.search(r"export\s+function\s+svg\b", src), (
        "dom.js must export svg"
    )


def test_modules_do_not_redefine_the_shared_dom_builder():
    """No deploy/assets module re-declares h()/svg() now that the shared dom.js
    owns the text-node DOM builder — they import from /assets/shared/js/dom.js
    instead. dom.js itself is the canonical definition and is exempt."""
    assert WEB_MODULE_FILES, "expected web ES modules to scan"
    offenders = []
    for path in WEB_MODULE_FILES:
        if path.resolve() == _SHARED_DOM_MODULE.resolve():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _LOCAL_DOM_BUILDER_DEF_RE.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "these modules redefine the shared text-node DOM builder — import "
        "h/svg from /assets/shared/js/dom.js instead (it is the one home for "
        "the XSS-safety primitive):\n" + "\n".join(offenders)
    )


# The CSRF/JSON fetch helpers (csrfHeaders / jsonHeaders) were promoted to the
# shared module at /assets/shared/js/http.js (same shared-by-promotion path as
# escape.js / dialog.js). The /sound/ editor used to carry a local copy; it now
# imports from http.js. This test keeps the duplication from creeping back: no
# canonical module may re-declare its own csrfHeaders/jsonHeaders again — http.js
# is the one home. Mirrors the escapeHtml drift guard above. (Matches function
# declarations and var/let/const assignments, NOT the `import { csrfHeaders,
# jsonHeaders }` statement, so importing the shared helpers stays allowed.)
_SHARED_HTTP_MODULE = Path("deploy/assets/shared/js/http.js")
_LOCAL_HTTP_HELPER_DEF_RE = re.compile(
    r"(?:function\s+(?:csrfHeaders|jsonHeaders)\b"
    r"|(?:var|let|const)\s+(?:csrfHeaders|jsonHeaders)\s*=)"
)


def test_shared_http_module_exists_and_exports_the_csrf_helpers():
    """The drift test below is only meaningful once the shared home exists and
    exports the names pages import."""
    assert _SHARED_HTTP_MODULE.is_file(), (
        f"{_SHARED_HTTP_MODULE} (shared CSRF/JSON fetch helpers) is missing"
    )
    src = _SHARED_HTTP_MODULE.read_text()
    assert re.search(r"export\s+function\s+csrfHeaders\b", src), (
        "http.js must export csrfHeaders"
    )
    assert re.search(r"export\s+function\s+jsonHeaders\b", src), (
        "http.js must export jsonHeaders"
    )


def test_modules_do_not_redefine_the_shared_csrf_helpers():
    """No deploy/assets module re-declares csrfHeaders/jsonHeaders now that the
    shared http.js owns them — they import from /assets/shared/js/http.js
    instead. http.js itself is the canonical definition and is exempt."""
    assert WEB_MODULE_FILES, "expected web ES modules to scan"
    offenders = []
    for path in WEB_MODULE_FILES:
        if path.resolve() == _SHARED_HTTP_MODULE.resolve():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _LOCAL_HTTP_HELPER_DEF_RE.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "these modules redefine the shared CSRF/JSON fetch helpers — import "
        "csrfHeaders / jsonHeaders from /assets/shared/js/http.js instead:\n"
        + "\n".join(offenders)
    )


# docs/UX-AUDIT-2026-09-03.md §5.5 — no inline style= in jasper/web/*.py HTML.
#
# Shrink-only: each entry is today's real style="/style=' count for that
# module. The test fails if a count GROWS (a new inline style=) and fails
# if a count SHRINKS without this table being lowered to match — a fix must
# update the allowlist in the same PR, never pass by accident. Delete an
# entry outright once its module reaches 0.
# Shrink-only ratchet: a new page never adds an entry (ADR-0253 section 4).
_INLINE_STYLE_ALLOWLIST = {}

_INLINE_STYLE_RE = re.compile(r"""style=["']""")


def test_web_pages_inline_style_counts_match_the_shrink_only_allowlist():
    counts = {}
    for path in WEB_PY_FILES:
        n = len(_INLINE_STYLE_RE.findall(path.read_text()))
        if n:
            counts[path.name] = n
    assert counts == _INLINE_STYLE_ALLOWLIST, (
        "inline style= counts drifted from the shrink-only allowlist "
        "(docs/UX-AUDIT-2026-09-03.md §5.5) — lower an entry as its page is "
        f"cleaned up, never raise one without a ledger row: {counts}"
    )


# §5.1: a row's label is its page's <title> and its header title, and the page
# goes back to the row's parent. Pages are scanned, not rendered — most need a
# live daemon; a page whose header is client-rendered (`chrome.js`) is checked
# on its <title> alone. A hub has no module: it is rendered (no daemon) and
# read back the same way.
_PAGE_MODULE = {
    "/sources/": "sources_setup",
    "/spotify/": "spotify_setup",
    "/bluetooth/": "bluetooth_setup",
    "/airplay/": "airplay_setup",
    "/sound/eq/": "sound_setup",
    "/sound/speaker/": "sound_setup",
    "/sound/output/": "sound_setup",
    "/sound/speaker/crossover/": "correction_crossover_flow",
    "/sound/bass/": "correction_bass_flow",
    "/sound/measurements/": "correction_measurements",
    "/assistant/voice/": "voice_page",
    "/assistant/wake/": "wake_setup",
    "/assistant/chat/": "chat_setup",
    "/assistant/tools/": "tools_setup",
    "/assistant/weather/": "weather_setup",
    "/assistant/transit/": "transit_page",
    "/assistant/google/": "google_setup",
    "/assistant/ha/": "home_assistant_setup",
    "/wifi/": "wifi_setup",
    "/sound/pair/": "rooms_setup",
    "/sound/pair/sync/": "sync_flow",
    "/system/": "system_setup",
    "/speaker/": "speaker_setup",
    "/wake-corpus/": "wake_corpus_setup",
}

# Shrink-only: every row whose page disagrees with its label today, against the
# ledger row (docs/UX-AUDIT-2026-09-03.md §7) that retires the entry. Drop an
# entry when its page is fixed; never add one without a ledger row. A `back`
# entry is B.2 re-parenting: the row now hangs under a hub while its page still
# links Home, and the Phase C row that moves the page fixes the link.
# Shrink-only ratchet: a new page never adds an entry (ADR-0253 section 4).
_TITLE_ALLOWLIST = {}

_SHELL_KIND = {"canonical_page": "title", "canonical_header": "header"}


def _scope(fn: ast.FunctionDef) -> tuple[dict, set, list]:
    """What `fn`'s names can be, its parameter names, and the calls it makes."""
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    names: dict[str, list] = {}
    for param, default in zip(
        positional[len(positional) - len(args.defaults):], args.defaults
    ):
        names[param.arg] = [default]
    for param, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            names[param.arg] = [default]
    calls = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            kwargs = {k.arg: k.value for k in node.keywords}
            calls.append((
                node.func.id,
                node.args[0] if node.args else kwargs.get("title"),
                kwargs.get("back_href", ...),
            ))
    return names, {p.arg for p in [*positional, *args.kwonlyargs]}, calls


def _literals(names: dict, node, resolve: bool = True) -> set[str]:
    """Strings `node` can be. An interpolated part becomes NUL, so an f-string
    URL still yields a comparable path."""
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.JoinedStr):
        return {"".join(
            v.value if isinstance(v, ast.Constant) else "\x00" for v in node.values
        )}
    if isinstance(node, ast.IfExp):
        return _literals(names, node.body, resolve) | _literals(
            names, node.orelse, resolve
        )
    if not (isinstance(node, ast.Name) and resolve):
        return set()
    found: set[str] = set()
    for value in names.get(node.id, ()):
        found |= _literals(names, value, resolve=False)
    return found


@functools.lru_cache(maxsize=None)
def _page_strings(module: str) -> tuple[frozenset, frozenset, frozenset]:
    """(<title>s, header titles, back-link paths) a page module can render."""
    tree = ast.parse(Path(f"jasper/web/{module}.py").read_text())
    scopes = [
        (fn.name, *_scope(fn))
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    # A module-local wrapper that forwards its own title parameter into a shell
    # renders that shell's title too, so it counts as one.
    kinds = {name: {kind} for name, kind in _SHELL_KIND.items()}
    growing = True
    while growing:
        growing = False
        for name, _names, params, calls in scopes:
            for callee, title, _back in calls:
                if not (isinstance(title, ast.Name) and title.id in params):
                    continue
                grown = kinds.get(name, set()) | kinds.get(callee, set())
                if grown != kinds.get(name, set()):
                    kinds[name] = grown
                    growing = True
    found = {"title": set(), "header": set()}
    backs: set[str] = set()
    for _name, names, _params, calls in scopes:
        for callee, title, back in calls:
            for kind in kinds.get(callee, ()):
                if title is not None:
                    found[kind] |= _literals(names, title)
                if kind == "header":
                    backs |= {"/"} if back is ... else _literals(names, back)
    paths = {urllib.parse.urlsplit(b).path or "/" for b in backs}
    return frozenset(found["title"]), frozenset(found["header"]), frozenset(paths)


@functools.lru_cache(maxsize=None)
def _hub_strings(path: str) -> tuple[frozenset, frozenset, frozenset]:
    """The same three strings, read off a rendered hub page."""
    page = render_hub(path, caps={}, app_css_version="testsha")
    return (
        frozenset(re.findall(r"<title>([^<]*)</title>", page)),
        frozenset(re.findall(r'<h1 class="app-header__title">([^<]*)</h1>', page)),
        frozenset(re.findall(r'<a class="icon-button" href="([^"]*)"', page)),
    )


def test_nav_row_labels_match_their_pages_or_the_shrink_only_allowlist():
    mismatched = {}
    for row in NAV:
        titles, headers, backs = (
            _hub_strings(row.path)
            if row.path in hub_paths()
            else _page_strings(_PAGE_MODULE[row.path])
        )
        bad = set()
        if row.label not in titles:
            bad.add("title")
        if headers and row.label not in headers:
            bad.add("header")
        if backs and row.parent not in backs:
            bad.add("back")
        if bad:
            mismatched[(row.path, row.label)] = bad

    assert mismatched == _TITLE_ALLOWLIST, (
        "landing label, <title>, header title and back link must agree "
        "(docs/web-ia.md §2, docs/UX-AUDIT-2026-09-03.md §5.1) — drop an "
        f"allowlist entry as its page is fixed: {mismatched}"
    )
