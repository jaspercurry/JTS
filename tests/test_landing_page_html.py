# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
_STREAMBOX_NGINX_PATH = _REPO / "deploy" / "nginx-jasper-streambox.conf"
_INSTALL_PATH = _REPO / "deploy" / "install.sh"
_WEB_ASSETS_LIB_PATH = _REPO / "deploy" / "lib" / "install" / "web-assets.sh"
_FONT_DIR = _REPO / "deploy" / "assets" / "fonts"
_APP_CSS_PATH = _REPO / "deploy" / "assets" / "app.css"


def _index_html() -> str:
    return _INDEX_PATH.read_text(encoding="utf-8")


def _app_css() -> str:
    return _APP_CSS_PATH.read_text(encoding="utf-8")


def _preflight_html() -> str:
    return _PREFLIGHT_PATH.read_text(encoding="utf-8")


def _nginx_location_block(nginx: str, location: str) -> str:
    match = re.search(
        rf"(?ms)^    {re.escape(location)} \{{\n(?P<body>.*?)^    \}}",
        nginx,
    )
    assert match is not None, f"missing nginx block: {location}"
    return match.group(0)


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


def test_volume_slider_surfaces_active_speaker_safety_muted_state() -> None:
    html = _index_html()
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    script = _volume_slider_script(html)

    assert 'id="volume-safety-note" hidden' in html
    assert "Speaker output is locked until active crossover setup is complete." in html
    assert 'href="/sound/"' in html
    assert ".volume-wrap.safety-muted" in style
    assert "cursor: not-allowed;" in style
    assert "fetch('/system/data.json', {cache: 'no-store'})" in script
    assert "active_speaker_output_safety" in script
    assert "typeof safety.safety_muted === 'boolean'" in script
    assert "typeof safety.volume_allowed === 'boolean'" in script
    assert "camilla.config_path" in script
    assert "active_speaker_staged_startup\\.yml" in script
    assert "var safetyMuted = false" in script
    assert "if (safetyMuted) return;" in script
    assert "aria-disabled" in script
    assert "hit.classList.toggle('safety-muted', safetyMuted)" in script
    assert "volume-safety-note" in script
    assert "fetch('/state'" not in script
    assert "disabled = true" not in script


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
          const el = {{
            id,
            style: {{}},
            textContent: '',
            attrs: {{}},
            classes: new Set(),
            listeners: {{}},
            setAttribute(name, value) {{ this.attrs[name] = String(value); }},
            removeAttribute(name) {{ delete this.attrs[name]; }},
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
          el.classList = {{
            toggle(name, force) {{
              if (force) el.classes.add(name);
              else el.classes.delete(name);
            }},
          }};
          return el;
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


def test_landing_page_capability_gates_fail_closed() -> None:
    html = _index_html()

    assert "caps[required] !== true" in html
    for line in html.splitlines():
        if "data-requires=" in line and line.lstrip().startswith("<"):
            assert "hidden" in line, line.strip()


def test_landing_page_bakes_capability_ceiling_at_first_paint() -> None:
    # The capability ceiling is install-time-static, so it's baked into the
    # page and applied SYNCHRONOUSLY at first paint — no /system/data.json
    # round-trip to lay out the page (that was the two-layer stutter), and
    # the layout survives a backend daemon being down.
    html = _index_html()

    # install.sh stamps this placeholder with the profile's capability map.
    assert "var BAKED_CAPS = __JTS_CAPS_JSON__;" in html
    assert "applyCapabilities(BAKED_CAPS);" in html

    # The snapshot poll must NOT re-drive layout (live values only), so a slow
    # or failed fetch can never blank or restyle the page.
    render = html.split("function renderSnapshot(snap)", 1)[1].split(
        "async function fetchSnapshot", 1,
    )[0]
    assert "applyCapabilities(" not in render


def test_install_bakes_landing_capabilities() -> None:
    # install.sh computes the profile's capability map from the SAME source
    # the runtime snapshot uses (system_capabilities_for_profile) and replaces
    # the placeholder, failing loud rather than shipping an unreplaced page.
    install = _INSTALL_PATH.read_text(encoding="utf-8")

    assert "system_capabilities_for_profile" in install
    assert "read_install_profile" in install
    assert "__JTS_CAPS_JSON__" in install
    assert "refusing to ship a broken page" in install


def test_landing_page_data_requires_match_capability_map() -> None:
    # Every data-requires="X" gate must have a key X in the capability map
    # (system_capabilities_for_profile) — otherwise applyCapabilities reads
    # caps["X"] === undefined, fails closed, and the section is hidden forever
    # with no error. Pin the seam so a typo'd or new gate fails the suite, not
    # silently in the field. (Cap keys are profile-independent — only the
    # boolean values differ — so checking one profile's keys is enough.)
    from jasper.install_profile import system_capabilities_for_profile

    used = set(re.findall(r'data-requires="([^"]+)"', _index_html()))
    assert used, "expected data-requires capability gates in the landing page"
    cap_keys = set(system_capabilities_for_profile("full"))
    missing = used - cap_keys
    assert not missing, (
        f"data-requires values with no capability-map key: {sorted(missing)}"
    )


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

    assert 'id="proceed" href="/correction/proceed?jts_cb=__APP_CSS_VERSION__"' in html
    assert "OK, proceed" in html
    assert "Your connection is not private" in html
    assert "Show Details" in html
    assert "Other JTS pages remain" not in html
    assert "new URLSearchParams(window.location.search" in html
    assert "new URL(requested, window.location.origin)" in html
    assert "'/correction/crossover/': '/crossover'" in html
    assert "Object.prototype.hasOwnProperty.call(allowed, path)" in html
    assert "Date.now().toString(36)" in html
    assert "'jts_cb='" in html
    assert "proceed.href = withCacheBust(proceedPath)" in html
    assert "window.location.hostname" not in html
    assert "https://jts.local/correction/" not in html


def test_room_correction_preflight_rejects_normalized_path_escape() -> None:
    html = _preflight_html()

    assert "parsed.pathname" in html
    assert "Object.prototype.hasOwnProperty.call(allowed, path)" in html
    assert "/correction/../sound/" not in html


def test_room_correction_preflight_uses_canonical_design() -> None:
    html = _preflight_html()

    # Migrated onto the canonical design system (the static-page analog of
    # canonical_page()): links the shared, SHA-busted stylesheet and uses
    # the .app-header / .btn / .info-card vocabulary.
    assert '/assets/app.css?v=__APP_CSS_VERSION__' in html
    assert 'class="app-header"' in html
    assert 'class="btn btn--primary"' in html
    assert 'href="#icon-back"' in html
    # The old hand-rolled look is gone: no Spotify-green buttons, no inline
    # system-font stack (both now come from app.css).
    assert "#1db954" not in html
    assert "-apple-system" not in html


def test_nginx_serves_correction_preflight_on_http_only() -> None:
    nginx = _NGINX_PATH.read_text(encoding="utf-8")
    preflight_block = _nginx_location_block(nginx, "location = /correction/")
    proceed_block = _nginx_location_block(nginx, "location = /correction/proceed")
    room_block = _nginx_location_block(nginx, "location = /correction/proceed/room")
    crossover_block = _nginx_location_block(
        nginx,
        "location = /correction/proceed/crossover",
    )
    bass_block = _nginx_location_block(nginx, "location = /correction/proceed/bass")
    https_block = _nginx_location_block(nginx, "location /correction/")

    assert "location = /correction" in nginx
    assert "return 302 /correction/;" in nginx
    assert "try_files /correction-preflight.html =404;" in preflight_block
    assert 'add_header Cache-Control "no-store";' in preflight_block
    assert "safe ?next=/correction/..." in nginx
    assert "return 302 https://$host/correction/$is_args$args;" in proceed_block
    assert "return 302 https://$host/correction/room/$is_args$args;" in room_block
    assert (
        "return 302 https://$host/correction/crossover/$is_args$args;"
        in crossover_block
    )
    assert "return 302 https://$host/correction/bass/$is_args$args;" in bass_block
    for block in (proceed_block, room_block, crossover_block, bass_block):
        assert 'add_header Cache-Control "no-store";' in block
    assert "proxy_pass http://127.0.0.1:8770/;" in https_block
    assert "return 302 http://$host$request_uri;" in nginx
    assert (
        'add_header Cache-Control "no-store";\n'
        "        return 302 http://$host$request_uri;"
    ) in nginx
    assert "Do not add HSTS here" in nginx
    assert "Strict-Transport-Security" not in nginx


def test_streambox_nginx_serves_hostname_safe_correction_proceed() -> None:
    nginx = _STREAMBOX_NGINX_PATH.read_text(encoding="utf-8")
    preflight_block = _nginx_location_block(nginx, "location = /correction/")
    proceed_block = _nginx_location_block(nginx, "location = /correction/proceed")
    crossover_block = _nginx_location_block(
        nginx,
        "location = /correction/proceed/crossover",
    )

    assert "try_files /correction-preflight.html =404;" in preflight_block
    assert 'add_header Cache-Control "no-store";' in preflight_block
    assert "return 302 https://$host/correction/$is_args$args;" in proceed_block
    assert (
        "return 302 https://$host/correction/crossover/$is_args$args;"
        in crossover_block
    )
    assert 'add_header Cache-Control "no-store";' in proceed_block
    assert 'add_header Cache-Control "no-store";' in crossover_block
    assert (
        'add_header Cache-Control "no-store";\n'
        "        return 302 http://$host$request_uri;"
    ) in nginx


def test_nginx_serves_static_management_assets() -> None:
    nginx = _NGINX_PATH.read_text(encoding="utf-8")

    assert "location /assets/" in nginx
    assert "root /usr/share/jasper-web;" in nginx
    assert "try_files $uri =404;" in nginx
    assert 'Cache-Control "public, max-age=31536000, immutable"' in nginx


def test_nginx_serves_assets_over_https_no_mixed_content() -> None:
    # The /correction/ measurement UI is the one wizard served over HTTPS
    # (getUserMedia needs a secure context) and links /assets/app.css + its
    # ES module by absolute path. The 443 server block must serve /assets/
    # itself; otherwise those subresources fall through to the downgrade
    # catch-all, 302 to HTTP, and browsers block them as mixed content —
    # leaving the page unstyled and its JS (mic capture, sweep) dead.
    nginx = _NGINX_PATH.read_text(encoding="utf-8")
    https_block = nginx[nginx.index("listen 443") :]

    assert "location /assets/" in https_block
    assert "location ~* ^/assets/.+\\.js$" in https_block
    # Must precede the HTTP-downgrade catch-all so assets are served, not
    # redirected.
    assert https_block.index("location /assets/") < https_block.index(
        "return 302 http://$host$request_uri;"
    )


def test_nginx_serves_sync_measurement_over_https() -> None:
    nginx = _NGINX_PATH.read_text(encoding="utf-8")
    https_block = nginx[nginx.index("listen 443") :]

    assert "location = /sync { return 308 /sync/; }" in https_block
    assert "location /sync/" in https_block
    sync_block = https_block[
        https_block.index("location /sync/") :
        https_block.index("# Static assets for the canonical look")
    ]
    assert "proxy_pass http://127.0.0.1:8770;" in sync_block
    assert "client_max_body_size 2m;" in sync_block
    assert "proxy_buffering off;" in sync_block
    assert "proxy_read_timeout 600s;" in sync_block
    assert https_block.index("location /sync/") < https_block.index(
        "return 302 http://$host$request_uri;"
    )


def test_install_copies_correction_preflight_page() -> None:
    install = _INSTALL_PATH.read_text(encoding="utf-8")

    assert "deploy/correction-preflight.html" in install
    assert "/usr/share/jasper-web/correction-preflight.html" in install


def test_install_stamps_preflight_app_css_version() -> None:
    # The preflight is static HTML linking the immutable app.css, so it
    # carries the __APP_CSS_VERSION__ placeholder and install.sh must
    # substitute it with the build SHA — exactly as it does for index.html.
    install = _INSTALL_PATH.read_text(encoding="utf-8")
    preflight = _preflight_html()

    assert "/assets/app.css?v=__APP_CSS_VERSION__" in preflight
    # Join bash `\`-continuations so the sed command and its target file
    # collapse onto one logical line. This tolerates a one-line or a
    # line-wrapped sed without coupling the test to install.sh's formatting.
    joined = re.sub(r"\\\n\s*", " ", install)
    assert any(
        "sed" in line
        and "__APP_CSS_VERSION__" in line
        and "correction-preflight.html" in line
        for line in joined.splitlines()
    ), "install.sh must sed __APP_CSS_VERSION__ into correction-preflight.html"


def test_install_prunes_retired_integrations_page() -> None:
    # The /integrations page was deleted; install.sh must remove the orphaned
    # file from previously-deployed Pis so it does not linger unreachable.
    install = _INSTALL_PATH.read_text(encoding="utf-8")
    assert "rm -f /usr/share/jasper-web/integrations.html" in install


def test_install_copies_landing_page_font_assets() -> None:
    # The copy lives in the web-assets lib (manifested, doctor-verified);
    # install.sh sources the lib and runs it.
    web_assets = _WEB_ASSETS_LIB_PATH.read_text(encoding="utf-8")

    assert 'deploy/assets/fonts/"*' in web_assets
    assert '${assets_root}/fonts/' in web_assets
    assert "deploy/lib/install/web-assets.sh" in _INSTALL_PATH.read_text(
        encoding="utf-8"
    )


def test_install_copies_and_stamps_app_css() -> None:
    # The copy lives in the web-assets lib; the cache-bust stamping stays
    # in install.sh (it rewrites index.html, not an asset).
    web_assets = _WEB_ASSETS_LIB_PATH.read_text(encoding="utf-8")
    install = _INSTALL_PATH.read_text(encoding="utf-8")

    assert "deploy/assets/app.css" in web_assets
    assert "${assets_root}/app.css" in web_assets
    # The static landing page's app.css link is cache-busted at install
    # time by substituting the build SHA into the version placeholder.
    assert "__APP_CSS_VERSION__" in _index_html()
    assert "s/__APP_CSS_VERSION__/" in install


def test_landing_page_stereo_pair_banner_wiring() -> None:
    """The pair banner: hidden by default, fed by GET /grouping (proxied by
    nginx to jasper-control), DOM-written via textContent only (untrusted
    leader_addr/channel never reach innerHTML), and the leader link is
    gated on a hostname-shaped value. On a follower the source selector
    hides and the slider relabels — its requests are forwarded server-side
    (jasper-control's bonded-follower volume proxy)."""
    html = _index_html()
    assert '<section class="control-section pair-banner" id="pair-banner" hidden>' in html
    assert 'id="source-section"' in html
    assert 'id="volume-eyebrow"' in html
    assert 'id="pair-manage-link" href="/rooms/" data-requires="pair_management" hidden' in html
    assert "fetch('/grouping')" in html
    assert "'Pair volume'" in html
    assert "import('/assets/shared/js/local-web-host.js')" in html
    assert "leaderLink.href = 'http://' + leaderHost + '/';" in html
    assert "leaderLink.href = 'http://' + g.leader_addr" not in html
    # The banner script writes text, never markup.
    pair_js = html.split("Stereo-pair banner", 1)[1].split("Source selector", 1)[0]
    assert "HOST_RE" not in pair_js
    assert "IPV4_RE" not in pair_js
    assert "function localWebHost" not in pair_js
    assert "innerHTML" not in pair_js
    # nginx exposes GET /grouping on the landing origin.
    nginx = _NGINX_PATH.read_text(encoding="utf-8")
    assert "location = /grouping" in nginx
