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
