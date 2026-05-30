"""Static guardrails for web wizard UI/security conventions.

These are intentionally narrow tripwires for patterns that have already
caused maintenance or safety debt. They do not try to lint all HTML/JS;
they keep future wizard changes aligned with the shared primitives in
jasper.web._common.
"""
from __future__ import annotations

import re
from pathlib import Path


WEB_SETUP_FILES = tuple(Path("jasper/web").glob("*_setup.py"))


def _matches(pattern: str) -> list[str]:
    rx = re.compile(pattern)
    hits = []
    for path in WEB_SETUP_FILES:
        text = path.read_text()
        if rx.search(text):
            hits.append(str(path))
    return hits


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
