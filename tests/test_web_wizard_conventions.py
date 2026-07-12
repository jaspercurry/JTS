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
import http
import inspect
import json
import re
import textwrap
from email.message import Message
from io import BytesIO
from pathlib import Path

from jasper.web import (
    google_setup,
    home_assistant_setup,
    spotify_setup,
    system_setup,
    wifi_setup,
)


WEB_SETUP_FILES = tuple(Path("jasper/web").glob("*_setup.py"))
WEB_PY_FILES = tuple(sorted(Path("jasper/web").glob("*.py")))

# DA-0217 migration allowlist. Each staged response-helper chunk shrinks this;
# the final stage deletes it and requires zero local JSON header assemblers.
_LEGACY_JSON_RESPONSE_ASSEMBLERS = {
    "bluetooth_setup.py",
    "correction_setup.py",
    "dial_setup.py",
    "rooms_setup.py",
    "sound_setup.py",
    "wake_corpus_setup.py",
    "wifi_setup.py",
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
    assert offenders == _LEGACY_JSON_RESPONSE_ASSEMBLERS


def test_migrated_local_object_responses_use_object_helper_not_byte_helper():
    for filename in (
        "home_assistant_setup.py",
        "sources_setup.py",
        "spotify_setup.py",
    ):
        source = (Path("jasper/web") / filename).read_text()
        assert "send_json_response(" in source
        assert "send_proxy_json(" not in source


# --- Mutating-request chokepoint: every wizard POST/DELETE handler funnels
# through the shared CSRF seam, and route-checks unknown paths FIRST.
#
# AGENTS.md "Web wizard conventions": every state-changing handler calls
# guard_mutating_request(), and "Route-check unknown POST paths before
# guard_mutating_request() so bogus paths return 404 without revealing CSRF
# state" (the convention block at the top of jasper/web/_common.py says the
# same). First run of the ordering guard caught wake_corpus_setup.py checking
# CSRF before routing in both do_POST and do_DELETE — bogus paths 403'd.

# wake_corpus_setup predates the shared double-submit seam and runs a
# reviewed bespoke scheme (server-held token + X-CSRF-Token header compare
# in _check_csrf). It is the only sanctioned exception to the
# guard_mutating_request chokepoint; do not grow this set.
_BESPOKE_CSRF_WIZARDS = {"wake_corpus_setup.py"}
_CSRF_GUARD_CALL_RE = re.compile(
    r"\bguard_mutating_request\s*\(|\b_check_csrf\s*\("
)


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


def _get_handlers():
    """Yield (path, func_name, source_segment) for every real wizard do_GET."""
    for path in WEB_PY_FILES:
        text = path.read_text()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.FunctionDef) and node.name == "do_GET":
                yield path, node.name, ast.get_source_segment(text, node)


def test_every_wizard_get_handler_uses_the_read_guard():
    handlers = list(_get_handlers())
    assert handlers, "expected wizard do_GET handlers to scan"
    offenders = []
    for path, name, seg in handlers:
        if path.name == "__main__.py":
            assert "_delegate" in seg, (
                f"{path}::{name} no longer delegates — it must call "
                "guard_read_request() itself"
            )
            continue
        if "guard_read_request" not in seg:
            offenders.append(f"{path}::{name}")
    assert offenders == [], (
        "wizard GET handlers that never call guard_read_request() "
        "(the shared Host + Fetch Metadata read chokepoint in "
        "jasper/web/_common.py):\n" + "\n".join(offenders)
    )


class _WizardGetRequest:
    """Drive a real wizard Handler instance without opening a socket."""

    def __init__(
        self,
        handler_cls,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        h = handler_cls.__new__(handler_cls)
        h.path = path
        h.headers = Message()
        h.headers["Content-Length"] = "0"
        for key, value in (headers or {}).items():
            h.headers[key] = value
        h.rfile = BytesIO()
        h.wfile = BytesIO()
        h.client_address = ("127.0.0.1", 0)

        self.status: int | None = None
        self.sent_headers: list[tuple[str, str]] = []
        self.wfile = h.wfile

        h.send_response = self._record_status
        h.send_response_only = self._record_status
        h.send_header = lambda name, value: self.sent_headers.append((name, value))
        h.end_headers = lambda: None
        h.send_error = self._record_status
        h.address_string = lambda: "127.0.0.1"
        h.log_message = lambda *a, **k: None
        self._handler = h

    def _record_status(self, status, *args, **kwargs):  # noqa: ANN001
        self.status = int(status)

    def do_GET(self):
        self._handler.do_GET()


def test_wizard_get_rejects_dns_rebinding_host():
    for handler_cls in (wifi_setup._make_handler(), system_setup._make_handler()):
        req = _WizardGetRequest(handler_cls, "/", headers={"Host": "evil.example"})
        req.do_GET()
        assert req.status == int(http.HTTPStatus.FORBIDDEN)
        assert b"host_not_allowed" in req.wfile.getvalue()


def test_wizard_get_rejects_cross_site_fetch_metadata():
    req = _WizardGetRequest(
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
        req = _WizardGetRequest(
            handler_cls,
            "/not-a-route",
            headers={"Host": "evil.example"},
        )
        req.do_GET()
        assert req.status == int(http.HTTPStatus.NOT_FOUND)


def test_wizard_get_allows_normal_management_host():
    for handler_cls in (wifi_setup._make_handler(), system_setup._make_handler()):
        req = _WizardGetRequest(handler_cls, "/", headers={"Host": "jts.local"})
        req.do_GET()
        assert req.status == int(http.HTTPStatus.OK)


def test_wizard_get_allows_cross_site_top_level_navigation():
    req = _WizardGetRequest(
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
    req = _WizardGetRequest(
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
    req = _WizardGetRequest(
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
        "client_id": "",
        "client_secret": "",
        "redirect_uri": (
            "https://jaspercurry.github.io/google-oauth-callback/?host=jts.local"
        ),
        "registry_path": "/tmp/jts-test-google-accounts.json",
    })


def test_legacy_msg_redirect_handlers_are_thin_shared_helper_delegates():
    for handler_cls in (_spotify_handler_cls(), _google_handler_cls()):
        source = textwrap.dedent(inspect.getsource(handler_cls._redirect))
        function = ast.parse(source).body[0]
        assert isinstance(function, ast.FunctionDef)
        assert len(function.body) == 1
        statement = function.body[0]
        assert isinstance(statement, ast.Expr)
        call = statement.value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Name)
        assert call.func.id == "redirect_with_legacy_msg"
        assert len(call.args) == 2
        assert all(isinstance(arg, ast.Name) for arg in call.args)
        assert [arg.id for arg in call.args] == [
            "self",
            "location",
        ]
        assert call.keywords == []


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
        req = _WizardGetRequest(handler_cls, path, headers=headers)
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
        (_spotify_handler_cls(), "/?msg=Linked+Spotify"),
        (_google_handler_cls(), "/?msg=Linked+Google"),
    )
    for handler_cls, path in cases:
        req = _WizardGetRequest(handler_cls, path, headers=headers)
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
        req = _WizardGetRequest(handler_cls, path, headers=headers)
        req.do_GET()
        assert req.status == int(http.HTTPStatus.FORBIDDEN)
        assert b"cross_site_request" in req.wfile.getvalue()


def test_every_wizard_mutating_handler_uses_the_csrf_chokepoint():
    handlers = list(_mutating_handlers())
    assert handlers, "expected wizard do_POST handlers to scan"
    offenders = []
    for path, name, seg in handlers:
        if path.name == "__main__.py":
            # The colocated-server router only delegates to the per-wizard
            # handlers (which each guard themselves) — assert it stays a
            # pure delegator rather than growing unguarded routes.
            assert "_delegate" in seg, (
                f"{path}::{name} no longer delegates — it must call "
                "guard_mutating_request() itself"
            )
            continue
        if path.name in _BESPOKE_CSRF_WIZARDS:
            assert "_check_csrf" in seg, (
                f"{path}::{name} lost its bespoke _check_csrf() call"
            )
            continue
        if "guard_mutating_request" not in seg:
            offenders.append(f"{path}::{name}")
    assert offenders == [], (
        "wizard mutating handlers that never call guard_mutating_request() "
        "(the shared Host/Origin + CSRF chokepoint in jasper/web/_common.py):\n"
        + "\n".join(offenders)
    )


def test_mutating_handlers_route_check_before_csrf_guard():
    """The first conditional in a do_POST/do_DELETE must be routing, never
    the CSRF guard: 'Route-check unknown POST paths before
    guard_mutating_request() so bogus paths return 404 without revealing
    CSRF state' (AGENTS.md / jasper/web/_common.py). In every compliant
    handler the first `if` tests the request path; a handler whose first
    branch is the guard 403s on bogus paths instead."""
    branch_re = re.compile(r"^\s*(?:if|elif)\b")
    offenders = []
    for path, name, seg in _mutating_handlers():
        if path.name == "__main__.py":
            continue  # pure delegator, asserted above
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


# AGENTS.md "Web wizard conventions": "Do not put untrusted strings into
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


def test_balance_measurement_uses_shared_audio_primitives():
    src = Path("deploy/assets/balance/js/main.js").read_text()

    assert "/assets/shared/js/measurement-audio.js" in src
    assert "createBandpassRmsMeter" in src
    assert "rmsToDbfs" in src
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


# The HTML-entity escaper (the five-char & < > " ' table) was copied across the
# wifi/bluetooth/dial/sound-profile/correction modules under two names
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
# escaped by the DOM. It was copy-pasted across the /chat/ and /system/ module
# graphs (and had already drifted — `catch (_)` vs `catch`, divergent comments)
# before it was promoted to the shared module at /assets/shared/js/dom.js (same
# shared-by-promotion path as dialog.js / escape.js / http.js). Pages now import
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
