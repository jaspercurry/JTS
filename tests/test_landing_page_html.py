# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Regression checks for the static landing page.

The markup is deploy/index.html; its behaviour is the ES module
deploy/assets/landing/js/main.js (capability gating and the status-*
sublabels it shares with the hub pages live in shared/js/settings-status.js).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from html.parser import HTMLParser
from pathlib import Path

import pytest

from jasper.web import wifi_setup
from jasper.web.landing import render_landing
from jasper.web.nav import hub_paths, render_hub

from . import nginx_site


_REPO = Path(__file__).resolve().parent.parent
_INDEX_PATH = _REPO / "deploy" / "index.html"
_LANDING_JS_PATH = _REPO / "deploy" / "assets" / "landing" / "js" / "main.js"
_SETTINGS_STATUS_JS_PATH = (
    _REPO / "deploy" / "assets" / "shared" / "js" / "settings-status.js"
)
_NGINX_PATH = nginx_site.PROFILE_CONFS["full"]
_STREAMBOX_NGINX_PATH = nginx_site.PROFILE_CONFS["streambox"]
_PROFILE_BY_CONF = {v: k for k, v in nginx_site.PROFILE_CONFS.items()}


def _index_html() -> str:
    """The document nginx serves: the template with install.sh's placeholders
    (icon sprite, caps island, control token, nav groups) substituted."""
    return render_landing(
        _INDEX_PATH.read_text(encoding="utf-8"),
        app_css_version="testsha",
        caps={},
        control_token="test-token",
    )


def _nginx_conf(conf_path: Path) -> str:
    """The profile's site conf with its deploy/nginx/ snippets resolved."""
    return nginx_site.conf_text(_PROFILE_BY_CONF[conf_path])


def _landing_js() -> str:
    return _LANDING_JS_PATH.read_text(encoding="utf-8")


_LOCATION_RX = nginx_site.LOCATION_RX
_nginx_servers = nginx_site.servers


def _proxy_upstream(block: str) -> str:
    """The `host:port` a location proxies to, without its mapped path."""
    match = re.search(r"proxy_pass +https?://([^/;\s]+)", block)
    assert match is not None, f"no proxy_pass in block: {block!r}"
    return match.group(1)


def _nginx_location_block(nginx: str, location: str) -> str:
    """The body of the first block matching an `location [<mod> ]<path>` header.

    Same parse as `_nginx_servers` — this is the by-header lookup on top of it.
    """
    modifier, _, path = location.removeprefix("location").strip().rpartition(" ")
    key = (modifier.strip(), path)
    for _ports, locations in _nginx_servers(nginx):
        if key in locations:
            return locations[key]
    raise AssertionError(f"missing nginx block: {location}")


def _conf_locations(conf: str) -> dict[frozenset[int], set[str]]:
    """Every `location` header in a conf, grouped by its server's listeners.

    Rendered back as written — `"= /sound"`, `"~* ^/assets/.+\\.js$"`, bare
    prefix `"/mic"` — so a conf that narrows a prefix block to an exact one
    reads as a difference rather than as parity. Keyed per listener because
    ADR-0253 §3 moves the `:80` and `:443` blocks alike: a union would read a
    route mounted on one listener only as parity with a conf that mounts it
    on both.
    """
    by_listener: dict[frozenset[int], set[str]] = {}
    for ports, locations in _nginx_servers(conf):
        by_listener.setdefault(ports, set()).update(
            f"{modifier} {path}".strip() for modifier, path in locations
        )
    return by_listener


def _assert_strong_no_cache(block: str) -> None:
    assert (
        'add_header Cache-Control "no-store, no-cache, max-age=0, must-revalidate" always;'
        in block
    )
    assert 'add_header Pragma "no-cache" always;' in block
    assert 'add_header Expires "0" always;' in block


def _volume_slider_script(js: str) -> str:
    start = js.index("// Volume slider.")
    end = js.index("// Stereo-pair banner.", start)
    return js[start:end]


def _volume_slider_dom_harness() -> str:
    """JS preamble shared by the volume-slider Node harnesses below: a
    minimal #vol-control/#vol-fill/#vol-percent DOM stand-in plus
    pointer-event/assert/delay helpers. A harness embeds this verbatim
    (via an f-string substitution, so its braces are not re-escaped) and
    adds its own `fetch` stub and assertions.
    """
    return """
function makeElement(id) {
  const el = {
    id,
    style: {},
    textContent: '',
    attrs: {},
    classes: new Set(),
    listeners: {},
    setAttribute(name, value) { this.attrs[name] = String(value); },
    removeAttribute(name) { delete this.attrs[name]; },
    getAttribute(name) { return this.attrs[name] || null; },
    addEventListener(type, fn) {
      (this.listeners[type] ||= []).push(fn);
    },
    getBoundingClientRect() {
      return { left: 100, top: 20, width: 200, height: 56 };
    },
    focus() { this.focused = true; },
    setPointerCapture(pointerId) { this.captured = pointerId; },
    releasePointerCapture(pointerId) { this.released = pointerId; },
  };
  el.classList = {
    toggle(name, force) {
      if (force) el.classes.add(name);
      else el.classes.delete(name);
    },
  };
  return el;
}

const elements = {
  'vol-control': makeElement('vol-control'),
  'vol-fill': makeElement('vol-fill'),
  'vol-percent': makeElement('vol-percent'),
};

elements['vol-control'].setAttribute('aria-valuenow', '50');
elements['vol-control'].setAttribute('aria-valuetext', '50%');
elements['vol-fill'].style.width = '50%';
elements['vol-percent'].textContent = '50%';

function pointerEvent(type, clientX) {
  return {
    type,
    clientX,
    pointerId: 7,
    pointerType: 'touch',
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
  };
}

function dispatch(type, e) {
  for (const fn of elements['vol-control'].listeners[type] || []) {
    fn(e);
  }
}

function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message}: expected ${expected}, got ${actual}`);
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
"""


def test_volume_slider_pointer_drag_updates_from_bar_coordinates(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the landing-page pointer harness")

    # The slider is a named init function in the module; the harness runs the
    # slice, then boots it exactly as main.js does.
    slider = _volume_slider_script(_landing_js()) + "\ninitVolume();\n"
    harness = textwrap.dedent(
        f"""
        const vm = require('node:vm');
        const script = {json.dumps(slider)};
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
            // Both http.js imports are stripped from the slice; the module
            // runs bare, so the harness supplies a stand-in for each.
            jsonHeaders: () => ({{ 'Content-Type': 'application/json' }}),
            startPolling(fn) {{ fn(); return () => {{}}; }},
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


def test_volume_slider_measurement_hold_state_machine(tmp_path: Path) -> None:
    """The measurement-hold lock is SERVER-derived on every /volume poll
    tick, never a local deadline — see poll()'s `holdOwnerFrom` and
    jasper/control/handlers/volume.py's `_refuse_authoritative_write`, whose
    409 body carries `owner` and `measurement`. Three transitions of the
    one state machine, pinned in sequence:

    (a) a 409 naming an owner locks the fader and shows who holds it;
    (b) the very next poll tick reporting the hold gone re-enables the
        fader and restores the percent — no local timer, no new endpoint;
    (c) a 409 carrying no owner (a different conflict, e.g. the
        active-speaker-setup block) falls back to the existing
        `markWriteFailed()` dash instead of locking the fader.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the landing-page pointer harness")

    slider = _volume_slider_script(_landing_js()) + "\ninitVolume();\n"
    harness = textwrap.dedent(
        f"""
        const vm = require('node:vm');
        const script = {json.dumps(slider)};
        const posted = [];
        const pollers = [];
        let volumeGets = 0;
        {_volume_slider_dom_harness()}
        function keyEvent(key) {{
          return {{ key, preventDefault() {{ this.defaultPrevented = true; }} }};
        }}

        function heldBody() {{
          return {{
            error: 'a measurement is in progress (owner=audio_measurement)',
            owner: 'audio_measurement',
            measurement: {{
              active: true, owner: 'audio_measurement', mode: 'gate',
              expires_in_s: 42.0, held_for_s: 3.2,
            }},
          }};
        }}

        function ownerlessConflictBody() {{
          return {{
            error: 'speaker output is not ready',
            active_speaker_setup: {{ detail: 'not confirmed' }},
          }};
        }}

        (async () => {{
          const context = {{
            document: {{
              visibilityState: 'visible',
              getElementById(id) {{ return elements[id]; }},
            }},
            fetch: async (url, options = {{}}) => {{
              if (url === '/volume/set') {{
                posted.push(JSON.parse(options.body));
                // (a) the first write lands on a live measurement's hold;
                // (c) every write after the fader unlocks hits a DIFFERENT
                // conflict — no owner — which must not read as a hold.
                const body = posted.length === 1 ? heldBody() : ownerlessConflictBody();
                return {{ ok: false, status: 409, json: async () => body }};
              }}
              if (url === '/volume') {{
                volumeGets += 1;
                // First tick is the boot poll; the second stands in for
                // the hold's own release() landing server-side.
                return {{
                  ok: true,
                  json: async () => ({{
                    percent: volumeGets < 2 ? 50 : 55,
                    measurement: {{
                      active: false, owner: null, mode: null,
                      expires_in_s: null, held_for_s: null,
                    }},
                  }}),
                }};
              }}
              return {{ ok: true, json: async () => ({{}}) }};
            }},
            jsonHeaders: () => ({{ 'Content-Type': 'application/json' }}),
            startPolling(fn) {{ pollers.push(fn); fn(); return () => {{}}; }},
            setTimeout,
            Promise,
            Date,
            Math,
            JSON,
          }};

          vm.runInNewContext(script, context, {{ timeout: 1000 }});
          await delay(0);

          // (a) named-owner 409 locks the fader and shows the incumbent.
          dispatch('pointerdown', pointerEvent('pointerdown', 150));
          await delay(200);
          assertEqual(posted.length, 1, 'one write attempted');
          assertEqual(
            elements['vol-percent'].textContent,
            'Held by audio_measurement for measurement',
            'held status text names the owner',
          );
          assertEqual(
            elements['vol-control'].classes.has('safety-muted'), true,
            'fader visually disabled while held',
          );
          assertEqual(
            elements['vol-control'].getAttribute('aria-disabled'), 'true',
            'fader marked aria-disabled while held',
          );

          // (b) the next poll tick reporting the hold gone re-enables the
          // fader and restores the percent — no local deadline involved.
          await pollers[0]();
          assertEqual(
            elements['vol-control'].classes.has('safety-muted'), false,
            'fader re-enabled the tick the server reports the hold gone',
          );
          assertEqual(
            elements['vol-control'].getAttribute('aria-disabled'), null,
            'aria-disabled cleared once unheld',
          );
          assertEqual(elements['vol-percent'].textContent, '55%', 'percent restored from the server');

          // (c) a 409 WITHOUT an owner is a different conflict; it falls
          // back to the ordinary write-failed dash, never the hold lock.
          dispatch('keydown', keyEvent('ArrowUp'));
          await delay(200);
          assertEqual(posted.length, 2, 'second write attempted after unlock');
          assertEqual(
            elements['vol-percent'].textContent, '\u2014',
            'ownerless conflict falls back to the write-failed dash',
          );
          assertEqual(
            elements['vol-control'].classes.has('safety-muted'), false,
            'ownerless conflict does not lock the fader',
          );
        }})().catch((err) => {{
          console.error(err && err.stack ? err.stack : err);
          process.exit(1);
        }});
        """
    )
    script_path = tmp_path / "volume_slider_measurement_hold_test.cjs"
    script_path.write_text(harness, encoding="utf-8")

    result = subprocess.run(
        [node, str(script_path)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_landing_page_uses_grouped_settings_rows() -> None:
    html = _index_html()

    assert "<title>JTS</title>" in html
    assert 'class="device-header"' not in html
    assert 'id="speaker-title"' not in html
    assert "JTS speaker" not in html
    assert "Manage your speaker" not in html
    assert "Voice & Skills" not in html
    assert 'class="setting-row"' in html
    headings = re.findall(
        r'<h2 class="eyebrow group-title" id="[^"]+">([^<]+)</h2>',
        html,
    )
    assert headings == ["Sources", "Sound", "Assistant", "System"]
def test_landing_page_capability_gates_fail_closed() -> None:
    html = _index_html()

    assert "caps[required] !== true" in _SETTINGS_STATUS_JS_PATH.read_text()
    for line in html.splitlines():
        if "data-requires=" in line and line.lstrip().startswith("<"):
            assert "hidden" in line, line.strip()


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


def test_streambox_shows_no_link_its_nginx_conf_cannot_serve() -> None:
    # A capability grant is what unhides a section, so widening one (streambox
    # gained ASSISTANT — ADR-0217) can reveal rows linking to wizards that
    # profile's nginx conf never routes: the household taps "Voice" and gets
    # the catch-all. Pin the seam between the two files rather than the three
    # sections that happened to break, so the next widened grant is caught.
    # The hubs hold most of those rows now, so they are walked too.
    from jasper.install_profile import system_capabilities_for_profile

    caps = system_capabilities_for_profile("streambox")
    conf = _nginx_conf(_STREAMBOX_NGINX_PATH)
    # An exact-match block serves that one path (`= /` is the landing page,
    # `= /sound/` the hub); a prefix block serves everything under it, except
    # the `/` catch-all, which is exactly what a dead link falls to. A link is
    # served if any listener serves it, so the listener groups are unioned.
    headers = {
        header for entries in _conf_locations(conf).values() for header in entries
    }
    exact = {h.removeprefix("= ") for h in headers if h.startswith("= ")}
    served = {h for h in headers if h.startswith("/") and h != "/"}

    class _Gates(HTMLParser):
        """Collect hrefs whose whole enclosing data-requires stack is granted."""

        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: list[tuple[str, str | None]] = []
            self.visible: list[str] = []

        def handle_starttag(self, tag, attrs):
            attrib = dict(attrs)
            gate = attrib.get("data-requires")
            href = attrib.get("href", "")
            if (
                href.startswith("/")
                and (gate is None or caps.get(gate) is True)
                and all(caps.get(g) is True for _, g in self.stack if g)
            ):
                self.visible.append(href.split("#")[0].split("?")[0])
            # Void elements never close, so they must not push a frame.
            if tag not in ("meta", "link", "img", "br", "hr", "input", "use"):
                self.stack.append((tag, gate))

        def handle_endtag(self, tag):
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    return

    gates = _Gates()
    gates.feed(_index_html())
    for hub in hub_paths():
        gates.feed(render_hub(hub, caps=caps, app_css_version="testsha"))
    unserved = sorted({
        href
        for href in gates.visible
        if href not in exact and not any(href.startswith(p) for p in served)
    })
    assert not unserved, (
        "landing and hub rows visible on streambox link to paths "
        f"nginx-jasper-streambox.conf does not serve: {unserved}"
    )


def test_nginx_serves_the_measurement_pages_over_plain_http() -> None:
    nginx = _nginx_conf(_NGINX_PATH)
    http_nginx = nginx[: nginx.index("listen 443")]
    https_nginx = nginx[nginx.index("listen 443") :]
    location = "location /sound/speaker/crossover/"
    http_proxy_block = _nginx_location_block(http_nginx, location)
    https_block = _nginx_location_block(https_nginx, location)

    # The views that do not capture audio stay reachable on plain HTTP — no
    # static interception, no scheme change (issue #2632).
    assert "proxy_pass http://127.0.0.1:8770/crossover/;" in http_proxy_block
    assert "client_max_body_size 32m;" in http_proxy_block
    assert "proxy_pass http://127.0.0.1:8770/crossover/;" in https_block
    assert "return 302 http://$host$request_uri;" in nginx
    catchall_block = _nginx_location_block(https_nginx, "location /")
    _assert_strong_no_cache(catchall_block)
    assert "return 302 http://$host$request_uri;" in catchall_block
    assert "Do not add HSTS here" in nginx
    assert "Strict-Transport-Security" not in nginx


def test_streambox_nginx_matches_plain_http_measurement_entry() -> None:
    nginx = _nginx_conf(_STREAMBOX_NGINX_PATH)
    http_nginx = nginx[: nginx.index("listen 443")]
    proxy_block = _nginx_location_block(
        http_nginx, "location /sound/speaker/crossover/"
    )

    assert "proxy_pass http://127.0.0.1:8770/crossover/;" in proxy_block
    https_nginx = nginx[nginx.index("listen 443") :]
    catchall_block = _nginx_location_block(https_nginx, "location /")
    _assert_strong_no_cache(catchall_block)
    assert "return 302 http://$host$request_uri;" in catchall_block


def test_both_nginx_profiles_have_canonical_sound_route_parity() -> None:
    # Public prefix -> what nginx leaves of it for the measurement backend.
    measurement_routes = {
        "speaker/crossover/": "crossover/",
        "bass/": "bass/",
        "measurements/": "measurements/",
    }
    for path in (_NGINX_PATH, _STREAMBOX_NGINX_PATH):
        nginx = _nginx_conf(path)
        https_at = nginx.index("listen 443")
        http_nginx = nginx[:https_at]
        https_nginx = nginx[https_at:]
        assert "\n+    location" not in nginx

        https_catchall = _nginx_location_block(https_nginx, "location /")
        assert "if ($request_method !~ ^(GET|HEAD)$) { return 405; }" in https_catchall
        _assert_strong_no_cache(https_catchall)

        compat = _nginx_location_block(http_nginx, "location /sound/")
        assert "proxy_pass http://127.0.0.1:8784/;" in compat
        assert "127.0.0.1:8770" not in compat
        assert "return 302" not in compat
        for mode in ("eq", "speaker", "output"):
            block = _nginx_location_block(http_nginx, f"location /sound/{mode}/")
            assert "proxy_pass http://127.0.0.1:8784/;" in block
            assert "127.0.0.1:8770" not in block
            assert f"proxy_set_header X-JTS-Sound-Page {mode};" in block

        for public_prefix, backend_prefix in measurement_routes.items():
            location = f"location /sound/{public_prefix}"
            for server_nginx in (http_nginx, https_nginx):
                block = _nginx_location_block(server_nginx, location)
                assert (
                    f"proxy_pass http://127.0.0.1:8770/{backend_prefix};"
                    in block
                )
                assert "client_max_body_size 32m;" in block
                assert "proxy_read_timeout 600s;" in block
                assert "127.0.0.1:8784" not in block

        # The alias namespace is deleted for good (audit §2, decision 4).
        assert not re.search(r"location\s+=?\s*/correction", nginx)


# The streambox profile ships no wake stack, so its conf mounts none of the
# wake surfaces; every other `location` must exist in both, on the same
# listener. Keyed by listener ports: all three are plain-HTTP mounts.
# Removal condition and the rest of the rule: ADR-0253 §7, ADR-0268.
_CONF_LOCATION_DIFF_ALLOWLIST = {
    frozenset({80}): frozenset({
        "/assistant/wake/",
        "/mic",
        "/wake-corpus/",
    }),
}


def test_both_nginx_profiles_mount_the_same_locations() -> None:
    """One conf may not gain a route the other silently misses.

    Per listener, because ADR-0253 §3 moves the `:80` and `:443` blocks
    alike: a route that reaches only one listener in one conf is drift, not
    parity. Every documented difference is speaker-only, so the streambox
    conf holds no location the speaker conf lacks in either direction.
    """
    speaker = _conf_locations(_nginx_conf(_NGINX_PATH))
    streambox = _conf_locations(_nginx_conf(_STREAMBOX_NGINX_PATH))

    assert sorted(map(sorted, speaker)) == sorted(map(sorted, streambox)), (
        "the two confs do not declare the same listeners"
    )
    for ports in speaker:
        allowed = _CONF_LOCATION_DIFF_ALLOWLIST.get(ports, frozenset())
        assert speaker[ports] - streambox[ports] == allowed, (
            f"speaker-only locations on {sorted(ports)}: "
            f"{sorted(speaker[ports] - streambox[ports])}"
        )
        assert streambox[ports] - speaker[ports] == set(), (
            f"streambox-only locations on {sorted(ports)}: "
            f"{sorted(streambox[ports] - speaker[ports])}"
        )


def test_every_proxying_block_includes_the_shared_proxy_headers() -> None:
    """A block that proxies carries the shared header snippet, not its own.

    The parity guard above sees the location set, not the bodies, so a block
    that hand-rolls `proxy_set_header` reads as parity while drifting from
    the snippet. Delete when the confs are generated from one source.
    """
    include = "include /etc/nginx/snippets/jts-proxy-headers.conf;"
    for path in (_NGINX_PATH, _STREAMBOX_NGINX_PATH):
        missing = sorted(
            f"{sorted(ports)} " + f"{modifier} {location}".strip()
            for ports, locations in _nginx_servers(_nginx_conf(path))
            for (modifier, location), body in locations.items()
            if "proxy_pass" in body and include not in body
        )
        assert not missing, (
            f"{path.name} blocks proxy without the shared headers: {missing}"
        )


# Every Assistant page under the hub prefix, and the upstream it reaches.
# `/wake/` is full-profile only: the streambox never gets WAKE_DETECTION.
_ASSISTANT_PAGES = {
    "/voice/": "127.0.0.1:8767",
    "/google/": "127.0.0.1:8768",
    "/wake/": "127.0.0.1:8774",
    "/transit/": "127.0.0.1:8777",
    "/ha/": "127.0.0.1:8778",
    "/weather/": "127.0.0.1:8779",
    "/tools/": "127.0.0.1:8786",
    "/chat/": "127.0.0.1:8787",
}


@pytest.mark.parametrize(
    "conf_path", (_NGINX_PATH, _STREAMBOX_NGINX_PATH), ids=lambda p: p.stem,
)
def test_assistant_pages_proxy_at_their_hub_path(conf_path: Path) -> None:
    """Each Assistant page is proxied at `/assistant/<name>/` to its upstream."""
    conf = _nginx_conf(conf_path)
    served = set()
    for ports, locations in _nginx_servers(conf):
        if 80 not in ports:
            continue
        for name, upstream in _ASSISTANT_PAGES.items():
            page = locations.get(("", f"/assistant{name}"))
            if page is None:
                continue
            assert _proxy_upstream(page) == upstream
            served.add(name)

    assert served == set(_ASSISTANT_PAGES) - (
        set() if conf_path == _NGINX_PATH else {"/wake/"}
    )
    # nginx refuses a conf with a duplicate location outright, and
    # `_nginx_servers` would quietly keep only the last one.
    for chunk in conf.split("\nserver {")[1:]:
        body = chunk[: chunk.index("\n}")] if "\n}" in chunk else chunk
        headers = [
            (m.group("mod") or "", m.group("path"))
            for m in _LOCATION_RX.finditer(body)
        ]
        duplicates = {h for h in headers if headers.count(h) > 1}
        assert not duplicates, duplicates


@pytest.mark.parametrize(
    "conf_path", (_NGINX_PATH, _STREAMBOX_NGINX_PATH), ids=lambda p: p.stem,
)
def test_google_oauth_callback_path_is_pinned_outside_the_wizard_prefix(
    conf_path: Path,
) -> None:
    """`/google/callback` is an externally registered URL, not an in-repo one.

    The bounce page at jaspercurry/google-oauth-callback sends the browser to
    `http://<host>/google/callback`; nothing in this repo can change where it
    lands. So the path gets its own exact block on the wizard's upstream,
    independent of whichever prefix the wizard itself is served under.
    """
    servers = _nginx_servers(_nginx_conf(conf_path))
    listeners = set()
    for ports, locations in servers:
        callback = locations.get(("=", "/google/callback"))
        if callback is None:
            continue
        assert "proxy_pass http://127.0.0.1:8768/callback;" in callback
        listeners |= set(ports)

    assert listeners == {80}


def test_both_nginx_profiles_allow_bounded_wifi_connect_rollback() -> None:
    for path in (_NGINX_PATH, _STREAMBOX_NGINX_PATH):
        nginx = _nginx_conf(path)
        wifi = _nginx_location_block(nginx, "location /wifi/")
        assert "proxy_pass http://127.0.0.1:8775/;" in wifi
        match = re.search(r"proxy_read_timeout (\d+)s;", wifi)
        assert match
        proxy_timeout = int(match.group(1))
        assert proxy_timeout >= wifi_setup.CONNECT_NEW_TIMEOUT_CEILING + 20


@pytest.mark.parametrize("path", hub_paths())
def test_both_nginx_profiles_serve_the_hubs_from_disk(path: str) -> None:
    # A hub is a static page rendered at install time, so its exact-match
    # block reads from disk like `location = /` — and only the exact match,
    # or the `/sound/` prefix proxy would stop serving the pages under it.
    for conf in (_NGINX_PATH, _STREAMBOX_NGINX_PATH):
        nginx = _nginx_conf(conf)
        hub = _nginx_location_block(nginx, f"location = {path}")
        bare = _nginx_location_block(nginx, f"location = {path.rstrip('/')}")

        assert "root /usr/share/jasper-web;" in hub
        assert f"try_files {path}index.html =404;" in hub
        assert 'add_header Cache-Control "no-store";' in hub
        assert "proxy_pass" not in hub
        assert f"return 302 {path};" in bare


def test_nginx_serves_static_management_assets() -> None:
    nginx = _nginx_conf(_NGINX_PATH)

    assert "location /assets/" in nginx
    assert "root /usr/share/jasper-web;" in nginx
    assert "try_files $uri =404;" in nginx
    assert 'Cache-Control "public, max-age=31536000, immutable"' in nginx


def test_nginx_serves_assets_over_https_no_mixed_content() -> None:
    # The measurement UI is served over HTTPS (getUserMedia needs a secure
    # context) and links /assets/app.css + its ES module by absolute path. The 443 server block must serve /assets/
    # itself; otherwise those subresources fall through to the downgrade
    # catch-all, 302 to HTTP, and browsers block them as mixed content —
    # leaving the page unstyled and its JS (mic capture, sweep) dead.
    nginx = _nginx_conf(_NGINX_PATH)
    https_block = nginx[nginx.index("listen 443") :]

    assert "location /assets/" in https_block
    assert "location ~* ^/assets/.+\\.js$" in https_block
    # Must precede the HTTP-downgrade catch-all so assets are served, not
    # redirected.
    assert https_block.index("location /assets/") < https_block.index(
        "return 302 http://$host$request_uri;"
    )


@pytest.mark.parametrize(
    "conf_path", (_NGINX_PATH, _STREAMBOX_NGINX_PATH), ids=lambda p: p.stem,
)
def test_speaker_timing_is_mounted_on_both_listeners(conf_path: Path) -> None:
    """`/sound/pair/sync/` rides both listeners in both profiles, on the same
    measurement backend as the crossover walk (docs/UX-AUDIT-2026-09-03.md §2).

    Mic capture needs the HTTPS origin, but a page mounted only there 404s on
    the plain-HTTP journey and invites a redirect into the self-signed origin
    (issue #2632) — so it is mounted on both, exactly as the walk is.
    """
    servers = _nginx_servers(_nginx_conf(conf_path))
    listeners = set()
    for ports, locations in servers:
        crossover = locations.get(("", "/sound/speaker/crossover/"))
        if crossover is None:
            continue
        sync = locations.get(("", "/sound/pair/sync/"))
        assert sync is not None, f"no /sound/pair/sync/ on listeners {set(ports)}"
        assert _proxy_upstream(sync) == _proxy_upstream(crossover)
        assert "proxy_pass http://127.0.0.1:8770/sync/;" in sync
        # A short mono marker capture, deliberately below the capture cap.
        assert "client_max_body_size 2m;" in sync
        assert "proxy_buffering off;" in sync
        assert "proxy_read_timeout 600s;" in sync
        assert "return 302" not in sync
        exact = locations[("=", "/sound/pair/sync")]
        assert exact.strip() == "return 308 /sound/pair/sync/;"
        listeners |= set(ports)

    assert listeners == {80, 443}


@pytest.mark.parametrize(
    "conf_path", (_NGINX_PATH, _STREAMBOX_NGINX_PATH), ids=lambda p: p.stem,
)
def test_the_split_sound_pages_keep_their_trailing_slash(conf_path: Path) -> None:
    """`/sound/speaker/` and `/sound/output/` are prefix blocks with the slash,
    plus a `location =` 308 for the bare path (ADR-0253 §3).

    The slash is load-bearing: a bare `location /sound/output` would also match
    /sound/output-topology, the live API the `/sound/` compat proxy serves, and
    a bare `location /sound/speaker` would swallow nothing today but has the
    same shape. The 308 is what makes the no-slash URL usable.
    """
    listeners = set()
    for ports, locations in _nginx_servers(_nginx_conf(conf_path)):
        if ("", "/sound/speaker/crossover/") in locations:
            # The child page rides both listeners, so its normaliser does too.
            assert locations[("=", "/sound/speaker/crossover")].strip() == (
                "return 308 /sound/speaker/crossover/;"
            )
            listeners |= set(ports)
        if ("", "/sound/") not in locations:
            continue
        for page in ("/sound/speaker/", "/sound/output/"):
            bare = page.rstrip("/")
            assert ("", page) in locations
            assert ("", bare) not in locations
            assert locations[("=", bare)].strip() == f"return 308 {page};"

    assert listeners == {80, 443}


@pytest.mark.parametrize(
    "conf_path", (_NGINX_PATH, _STREAMBOX_NGINX_PATH), ids=lambda p: p.stem,
)
def test_no_conf_still_mounts_the_old_sync_path(conf_path: Path) -> None:
    """The move is a move: no redirect and no compat block left behind."""
    stale = [
        (mod, path)
        for _ports, locations in _nginx_servers(_nginx_conf(conf_path))
        for mod, path in locations
        if path == "/sync" or path.startswith("/sync/")
    ]

    assert stale == []


def test_landing_page_stereo_pair_banner_wiring() -> None:
    """The pair banner: hidden by default, fed by GET /grouping (proxied by
    nginx to jasper-control), DOM-written via textContent only (untrusted
    leader_addr/channel never reach innerHTML), and the leader link is
    gated on a hostname-shaped value. On a follower the source selector
    hides and the slider relabels — its requests are forwarded server-side
    (jasper-control's bonded-follower volume proxy)."""
    html = _index_html()
    js = _landing_js()
    assert '<section class="control-section pair-banner" id="pair-banner" hidden>' in html
    assert 'id="source-section"' in html
    assert 'id="volume-eyebrow"' in html
    assert 'id="pair-manage-link" href="/sound/pair/" data-requires="pair_management" hidden' in html
    assert "fetch('/grouping')" in js
    assert "'Pair volume'" in js
    assert 'from "/assets/shared/js/local-web-host.js"' in js
    assert "leaderLink.href = 'http://' + leaderHost + '/';" in js
    assert "leaderLink.href = 'http://' + g.leader_addr" not in js
    # The banner script writes text, never markup.
    pair_js = js.split("Stereo-pair banner", 1)[1].split("Source selector", 1)[0]
    assert "HOST_RE" not in pair_js
    assert "IPV4_RE" not in pair_js
    assert "function localWebHost" not in pair_js
    assert "innerHTML" not in pair_js
    # nginx exposes GET /grouping on the landing origin.
    nginx = _nginx_conf(_NGINX_PATH)
    assert "location = /grouping" in nginx
