"""Static guardrails for web wizard UI/security conventions.

These are intentionally narrow tripwires for patterns that have already
caused maintenance or safety debt. They do not try to lint all HTML/JS;
they keep future wizard changes aligned with the shared primitives in
jasper.web._common.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path


WEB_SETUP_FILES = tuple(Path("jasper/web").glob("*_setup.py"))
WEB_PY_FILES = tuple(sorted(Path("jasper/web").glob("*.py")))


def _matches(pattern: str) -> list[str]:
    rx = re.compile(pattern)
    hits = []
    for path in WEB_SETUP_FILES:
        text = path.read_text()
        if rx.search(text):
            hits.append(str(path))
    return hits


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


# Redesigned pages (/system/, /sound/) deliver their behaviour as static ES
# modules under deploy/assets/<page>/js/ — outside the *_setup.py scan above.
WEB_MODULE_FILES = tuple(Path("deploy/assets").glob("*/js/*.js"))


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


# The same retirement applies to the legacy wrap_page()/hand-rolled wizards,
# whose inline JS lives in Python string literals. There the helper is the
# inline twin in _common (jtsConfirm/jtsAlert/jtsConfirmSubmit), injected by
# wrap_page() or embedded by hand-rolled pages. Two invariants below.

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
        "(jasper.web._common.dialog_helpers_js) instead:\n" + "\n".join(offenders)
    )


def test_wizards_using_dialog_helper_have_it_wired():
    """A page that calls jtsConfirm/jtsAlert/jtsConfirmSubmit must actually get
    the helper JS on the page — via the shared wrap_page() (which injects it) or
    by embedding dialog_helpers_js() itself (hand-rolled pages). Otherwise the
    call is a runtime ReferenceError — the inert-conversion bug surfaced during
    the migration, where a page's local _wrap() hand-rolled the shell without
    the helper."""
    uses_helper = re.compile(r"\bjts(?:Confirm|Alert|ConfirmSubmit)\s*\(")
    # \bwrap_page\( matches the SHARED wrap_page() call but NOT a local
    # _wrap_page( (no word boundary after the leading underscore).
    wired = re.compile(r"\bwrap_page\s*\(|\bdialog_helpers_js\b")
    offenders = [
        str(path) for path in WEB_SETUP_FILES
        if uses_helper.search(text := path.read_text()) and not wired.search(text)
    ]
    assert offenders == [], (
        "these wizards call jtsConfirm/jtsAlert but never wire the helper (no "
        "wrap_page() call, no dialog_helpers_js() embed) — the dialog would be a "
        "ReferenceError at runtime:\n" + "\n".join(offenders)
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
