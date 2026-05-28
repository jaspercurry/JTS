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
_PREFLIGHT_PATH = _REPO / "deploy" / "correction-preflight.html"
_NGINX_PATH = _REPO / "deploy" / "nginx-jasper.conf"
_INSTALL_PATH = _REPO / "deploy" / "install.sh"


def _index_html() -> str:
    return _INDEX_PATH.read_text(encoding="utf-8")


def _preflight_html() -> str:
    return _PREFLIGHT_PATH.read_text(encoding="utf-8")


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


def test_landing_page_has_source_selector_buttons() -> None:
    html = _index_html()

    assert 'aria-label="Playback source"' in html
    for source in ("auto", "airplay", "bluetooth", "spotify", "usbsink"):
        assert f'data-source="{source}"' in html


def test_landing_page_uses_grouped_settings_rows() -> None:
    html = _index_html()

    assert "<title>JTS</title>" in html
    assert "JTS speaker" not in html
    assert "Manage your speaker" not in html
    assert "Voice & Skills" not in html
    for heading in (
        "Sources",
        "Assistant",
        "Sound",
        "Network",
        "Accessories",
        "System",
    ):
        assert f">{heading}</h2>" in html
    assert 'class="setting-row"' in html
    headings = re.findall(r"<h2[^>]*>([^<]+)</h2>", html)
    assert headings == [
        "Sources",
        "Assistant",
        "Sound",
        "Network",
        "Accessories",
        "System",
    ]
    assert "snap.satellites" not in html


def test_landing_page_css_keeps_type_stable() -> None:
    html = _index_html()
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]

    assert "vw" not in style
    for value in re.findall(r"letter-spacing:\s*([^;]+);", style):
        assert value.strip() == "0"


def test_source_selector_uses_control_endpoints() -> None:
    html = _index_html()

    assert "fetch('/source/state'" in html
    assert "fetch('/source/select'" in html
    assert "pendingSource" in html
    assert "source-button.playing::after" in html


def test_room_correction_card_uses_http_preflight() -> None:
    html = _index_html()

    assert 'id="correction-card" href="/correction/"' in html
    assert "data-https" not in html
    assert "HTTPS warning" in html
    assert "walkthrough" in html


def test_room_correction_preflight_switches_to_https() -> None:
    html = _preflight_html()

    assert 'id="proceed"' in html
    assert "OK, proceed" in html
    assert "Your connection is not private" in html
    assert "Show Details" in html
    assert "Other JTS pages remain" not in html
    assert "https://' + window.location.hostname + '/correction/'" in html


def test_nginx_serves_correction_preflight_on_http_only() -> None:
    nginx = _NGINX_PATH.read_text(encoding="utf-8")

    assert "location = /correction" in nginx
    assert "return 308 /correction/;" in nginx
    assert "location = /correction/" in nginx
    assert "try_files /correction-preflight.html =404;" in nginx
    assert "location /correction/" in nginx
    assert "proxy_pass http://127.0.0.1:8770/;" in nginx
    assert "return 308 http://$host$request_uri;" in nginx
    assert "Do not add HSTS here" in nginx
    assert "Strict-Transport-Security" not in nginx


def test_install_copies_correction_preflight_page() -> None:
    install = _INSTALL_PATH.read_text(encoding="utf-8")

    assert "deploy/correction-preflight.html" in install
    assert "/usr/share/jasper-web/correction-preflight.html" in install
