"""Regression checks for the static landing page.

The main page is plain HTML/JS under deploy/index.html. These tests
pin the small optimistic-volume state machine so stale POST responses
or polls cannot repaint an older volume while a newer local gesture is
still pending.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent
_INDEX_PATH = _REPO / "deploy" / "index.html"
_PREFLIGHT_PATH = _REPO / "deploy" / "correction-preflight.html"
_NGINX_PATH = _REPO / "deploy" / "nginx-jasper.conf"
_INSTALL_PATH = _REPO / "deploy" / "install.sh"
_FONT_DIR = _REPO / "deploy" / "assets" / "fonts"
_APP_CSS_PATH = _REPO / "deploy" / "assets" / "app.css"


def _index_html() -> str:
    return _INDEX_PATH.read_text(encoding="utf-8")


def _app_css() -> str:
    return _APP_CSS_PATH.read_text(encoding="utf-8")


def _preflight_html() -> str:
    return _PREFLIGHT_PATH.read_text(encoding="utf-8")


def _volume_slider_script(html: str) -> str:
    start = html.index("    // Volume slider.")
    end = html.index("    // Source selector.", start)
    return html[start:end]


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


def test_volume_slider_uses_touch_friendly_pointer_target() -> None:
    html = _index_html()

    assert 'id="vol-control"' in html
    assert 'role="slider"' in html
    assert "touch-action: none;" in html
    assert "function xToPercent(clientX)" in html
    assert "hit.setPointerCapture(e.pointerId)" in html
    assert "hit.addEventListener('pointermove'" in html
    assert 'id="vol-input"' not in html
    assert 'type="range"' not in html


def test_volume_slider_pointer_drag_updates_from_bar_coordinates(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the landing-page pointer harness")

    harness = textwrap.dedent(
        f"""
        const vm = require('node:vm');
        const script = {json.dumps(_volume_slider_script(_index_html()))};
        const posted = [];

        function makeElement(id) {{
          return {{
            id,
            style: {{}},
            textContent: '',
            attrs: {{}},
            listeners: {{}},
            setAttribute(name, value) {{ this.attrs[name] = String(value); }},
            getAttribute(name) {{ return this.attrs[name] || null; }},
            addEventListener(type, fn) {{
              (this.listeners[type] ||= []).push(fn);
            }},
            getBoundingClientRect() {{
              return {{ left: 100, top: 20, width: 200, height: 56 }};
            }},
            focus() {{ this.focused = true; }},
            setPointerCapture(pointerId) {{ this.captured = pointerId; }},
            releasePointerCapture(pointerId) {{ this.released = pointerId; }},
          }};
        }}

        const elements = {{
          'vol-control': makeElement('vol-control'),
          'vol-fill': makeElement('vol-fill'),
          'vol-percent': makeElement('vol-percent'),
        }};

        elements['vol-control'].setAttribute('aria-valuenow', '50');
        elements['vol-control'].setAttribute('aria-valuetext', '50%');
        elements['vol-fill'].style.width = '50%';
        elements['vol-percent'].textContent = '50%';

        function event(type, clientX) {{
          return {{
            type,
            clientX,
            pointerId: 7,
            pointerType: 'touch',
            defaultPrevented: false,
            preventDefault() {{ this.defaultPrevented = true; }},
          }};
        }}

        function dispatch(type, e) {{
          for (const fn of elements['vol-control'].listeners[type] || []) {{
            fn(e);
          }}
        }}

        function assertEqual(actual, expected, message) {{
          if (actual !== expected) {{
            throw new Error(`${{message}}: expected ${{expected}}, got ${{actual}}`);
          }}
        }}

        function delay(ms) {{
          return new Promise((resolve) => setTimeout(resolve, ms));
        }}

        (async () => {{
          const context = {{
            document: {{
              visibilityState: 'visible',
              getElementById(id) {{ return elements[id]; }},
            }},
            fetch: async (url, options = {{}}) => {{
              if (url === '/volume/set') posted.push(JSON.parse(options.body));
              return {{ ok: true, json: async () => ({{ percent: 50 }}) }};
            }},
            setInterval() {{ return 1; }},
            setTimeout,
            Promise,
            Date,
            Math,
            JSON,
          }};

          vm.runInNewContext(script, context, {{ timeout: 1000 }});
          await delay(0);

          const down = event('pointerdown', 150);
          dispatch('pointerdown', down);
          assertEqual(down.defaultPrevented, true, 'pointerdown prevents page gesture');
          assertEqual(elements['vol-control'].captured, 7, 'pointer capture id');
          assertEqual(elements['vol-control'].getAttribute('aria-valuenow'), '25', 'pointerdown value');
          assertEqual(elements['vol-percent'].textContent, '25%', 'pointerdown label');
          assertEqual(elements['vol-fill'].style.width, '25%', 'pointerdown fill');

          const move = event('pointermove', 260);
          dispatch('pointermove', move);
          assertEqual(move.defaultPrevented, true, 'pointermove prevents page gesture');
          assertEqual(elements['vol-control'].getAttribute('aria-valuenow'), '80', 'pointermove value');
          assertEqual(elements['vol-percent'].textContent, '80%', 'pointermove label');
          assertEqual(elements['vol-fill'].style.width, '80%', 'pointermove fill');

          dispatch('pointerup', event('pointerup', 320));
          assertEqual(elements['vol-control'].released, 7, 'pointer release id');
          assertEqual(elements['vol-control'].getAttribute('aria-valuenow'), '100', 'pointerup clamps high');

          await delay(200);
          assertEqual(posted.at(-1).percent, 100, 'latest posted percent');
        }})().catch((err) => {{
          console.error(err && err.stack ? err.stack : err);
          process.exit(1);
        }});
        """
    )
    script_path = tmp_path / "volume_slider_pointer_test.cjs"
    script_path.write_text(harness, encoding="utf-8")

    result = subprocess.run(
        [node, str(script_path)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_landing_page_has_source_selector_buttons() -> None:
    html = _index_html()
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]

    assert 'aria-label="Playback source"' in html
    for source in ("auto", "airplay", "bluetooth", "spotify", "usbsink"):
        assert f'data-source="{source}"' in html
    assert re.search(r"\.source-buttons \{[^}]*\bgap: 4px;", style)
    assert re.search(
        r"\.source-button\.playing::after \{[^}]*\btop: 10px;[^}]*\bright: 10px;",
        style,
    )


def test_landing_page_uses_grouped_settings_rows() -> None:
    html = _index_html()

    assert "<title>JTS</title>" in html
    assert 'class="device-header"' not in html
    assert 'id="speaker-title"' not in html
    assert "JTS speaker" not in html
    assert "Manage your speaker" not in html
    assert "Voice & Skills" not in html
    for heading in (
        "Sources",
        "Sound",
        "Assistant",
        "Integrations",
        "Network",
        "Accessories",
        "System",
    ):
        assert f">{heading}</h2>" in html
    assert 'class="setting-row"' in html
    headings = re.findall(
        r'<h2 class="eyebrow group-title" id="[^"]+">([^<]+)</h2>',
        html,
    )
    assert headings == [
        "Sources",
        "Sound",
        "Assistant",
        "Integrations",
        "Network",
        "Accessories",
        "System",
    ]
    assert "snap.satellites" not in html


def test_landing_page_tracks_static_reference_visual_tokens() -> None:
    # Tokens and the .page container now live in the shared stylesheet
    # (the landing page links it); only landing-specific bits stay inline.
    html = _index_html()
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    app_css = _app_css()

    assert '<link rel="stylesheet" href="/assets/app.css' in html
    assert "--background: oklch(0.961 0.014 80);" in app_css
    assert "--primary: oklch(0.64 0.062 142);" in app_css
    assert "max-width: 48rem;" in app_css
    assert "padding: 2rem 1.5rem 6rem;" in app_css
    assert ".hero { padding: 2rem 0; }" in style
    assert '<section class="hero" aria-label="Primary controls">' in html
    assert 'class="footer-pill"' in html


def test_landing_page_uses_local_font_assets_only() -> None:
    # @font-face moved to the shared stylesheet; the page must still avoid
    # external font CDNs and the local woff2 files must exist.
    html = _index_html()
    app_css = _app_css()

    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert "fonts.googleapis.com" not in app_css
    assert "fonts.gstatic.com" not in app_css
    assert '@font-face' in app_css
    assert 'font-family: "Figtree"' in app_css
    assert 'font-family: "Outfit"' in app_css
    for filename in (
        "figtree-latin.woff2",
        "figtree-latin-ext.woff2",
        "outfit-latin.woff2",
        "outfit-latin-ext.woff2",
        "OFL-Figtree.txt",
        "OFL-Outfit.txt",
    ):
        path = _FONT_DIR / filename
        assert path.is_file()
        assert path.stat().st_size > 0


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


def test_nginx_serves_static_management_assets() -> None:
    nginx = _NGINX_PATH.read_text(encoding="utf-8")

    assert "location /assets/" in nginx
    assert "root /usr/share/jasper-web;" in nginx
    assert "try_files $uri =404;" in nginx
    assert 'Cache-Control "public, max-age=31536000, immutable"' in nginx


def test_install_copies_correction_preflight_page() -> None:
    install = _INSTALL_PATH.read_text(encoding="utf-8")

    assert "deploy/correction-preflight.html" in install
    assert "/usr/share/jasper-web/correction-preflight.html" in install


def test_install_copies_landing_page_font_assets() -> None:
    install = _INSTALL_PATH.read_text(encoding="utf-8")

    assert "/usr/share/jasper-web/assets/fonts" in install
    assert 'deploy/assets/fonts/"*' in install


def test_install_copies_and_stamps_app_css() -> None:
    install = _INSTALL_PATH.read_text(encoding="utf-8")

    assert "deploy/assets/app.css" in install
    assert "/usr/share/jasper-web/assets/app.css" in install
    # The static landing page's app.css link is cache-busted at install
    # time by substituting the build SHA into the version placeholder.
    assert "__APP_CSS_VERSION__" in _index_html()
    assert "s/__APP_CSS_VERSION__/" in install
