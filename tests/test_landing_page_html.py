"""Regression checks for the static landing page.

The main page is plain HTML/JS under deploy/index.html. These tests
pin the small optimistic-volume state machine so stale POST responses
or polls cannot repaint an older volume while a newer local gesture is
still pending.
"""
from __future__ import annotations

import re
from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent
_INDEX_PATH = _REPO / "deploy" / "index.html"


def _index_html() -> str:
    return _INDEX_PATH.read_text(encoding="utf-8")


def test_volume_slider_suppresses_poll_while_local_write_pending() -> None:
    html = _index_html()

    assert "function localVolumeDirty()" in html
    assert "dragging || flushing || inFlight || pending !== null" in html
    assert "Date.now() < ignorePollUntil" in html
    assert re.search(
        r"async function poll\(\) \{\s+if \(localVolumeDirty\(\)\) return;",
        html,
    )


def test_volume_slider_ignores_stale_post_responses() -> None:
    html = _index_html()

    assert "var desiredPct = null" in html
    assert re.search(
        r"if \(!dragging && pending === null && toSend === desiredPct &&\s+"
        r"typeof data\.percent === 'number'\) \{\s+setUI\(data\.percent\);",
        html,
    )


def test_volume_slider_allows_only_one_flush_loop() -> None:
    html = _index_html()

    assert "var flushing = false" in html
    assert "if (flushing) return;" in html
    assert "flushing = true;" in html
    assert "flushing = false;" in html
