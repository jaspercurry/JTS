"""Canonical-design-system tests for the migrated /weather/ wizard.

Two concerns:

1. The page renders canonical design-system bytes (links /assets/app.css
   and its page CSS, carries the shared .app-header, embeds the CSRF meta
   tag, uses the .field/.form-actions form vocabulary). It is a plain
   server-rendered request/response form, so it ships no ES module.
2. The migration was presentation-only: the routes (GET /, POST /save,
   POST /clear), the CSRF handshake, the flash redirects, and the public
   module surface (render fn, make_server, main) are unchanged. The
   request/response *logic* (geocode probe, transit coupling, voice
   restart) is already covered by tests/test_weather_setup.py; here we
   assert the routes still resolve through a live server.
"""
from __future__ import annotations

import socket
import threading

import pytest

import jasper.location_state as ls
from jasper.web import _common, weather_setup

from ._web_test_helpers import make_csrf_session, post_with_csrf


# The page reads weather/transit env vars as a *fallback* when its state file
# is empty (production behaviour). Clear them so render tests see a known empty
# state regardless of the ambient environment or test ordering (a host with a
# configured speaker, or a sibling test, must not leak a location in).
_FALLBACK_ENV_VARS = (
    ls.WEATHER_LAT_ENV, ls.WEATHER_LON_ENV, ls.WEATHER_DISPLAY_NAME_ENV,
    ls.WEATHER_DEFAULT_LOCATION_ENV, ls.WEATHER_UNITS_ENV,
    ls.TRANSIT_LAT_ENV, ls.TRANSIT_LON_ENV, ls.TRANSIT_DISPLAY_NAME_ENV,
)


@pytest.fixture(autouse=True)
def _clear_location_env(monkeypatch):
    for name in _FALLBACK_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# --- render (no server needed) ---------------------------------------------

def _render(weather_state=None, transit_state=None, csrf_token="x" * 43,
            status_msg="", back_href="/") -> str:
    return weather_setup._index_html(
        weather_state or {},
        transit_state or {},
        csrf_token,
        status_msg=status_msg,
        back_href=back_href,
    ).decode()


def test_render_is_canonical_document():
    out = _render()
    assert out.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in out


def test_render_links_page_css():
    out = _render()
    assert weather_setup.WEATHER_PAGE_CSS_HREF == "/assets/weather/weather.css"
    assert "/assets/weather/weather.css?v=" in out


def test_render_has_shared_app_header():
    out = _render()
    assert 'class="app-header"' in out
    assert '<h1 class="app-header__title">Weather</h1>' in out
    assert '<use href="#icon-back">' in out


def test_render_honors_safe_back_href():
    out = _render(back_href="/tools/pack/weather/")
    assert 'href="/tools/pack/weather/"' in out


def test_render_embeds_csrf_meta_and_keeps_form_fields():
    token = "z" * 43
    out = _render(csrf_token=token)
    assert 'meta name="jts-csrf"' in out
    assert f'content="{token}"' in out
    # Both the save form and the clear form keep a hidden CSRF field.
    assert out.count(f'name="{_common.CSRF_FORM_FIELD}"') == 2


def test_render_uses_canonical_field_vocabulary():
    out = _render()
    assert 'class="field"' in out
    assert 'class="form-actions"' in out
    assert 'class="form-hint"' in out
    assert 'class="btn btn--primary"' in out
    assert 'class="btn btn--danger"' in out


def test_render_form_actions_unchanged():
    out = _render()
    assert 'action="./save"' in out
    assert 'action="./clear"' in out
    assert 'method="post"' in out


def test_render_keeps_units_select_and_manual_details():
    out = _render()
    assert '<select id="units" name="units">' in out
    assert ">Celsius<" in out and ">Fahrenheit<" in out
    assert "<details" in out
    assert 'name="manual_lat"' in out and 'name="manual_lon"' in out


def test_render_keeps_geocode_privacy_disclosure():
    out = _render()
    # Load-bearing for the OSM/Nominatim usage policy.
    assert "nominatim.openstreetmap.org" in out
    assert "operations.osmfoundation.org" in out


def test_render_ships_no_inline_script_or_style():
    out = _render()
    assert "<script" not in out
    assert "<style>" not in out


def test_render_no_legacy_chrome():
    out = _render()
    assert "wrap" "_page" not in out
    assert "location-result" not in out


def test_render_saved_weather_location_card():
    state = {
        weather_setup.LAT_ENV: "40.700",
        weather_setup.LON_ENV: "-74.000",
        weather_setup.DISPLAY_NAME_ENV: "Brooklyn, NY",
    }
    out = _render(weather_state=state)
    assert "Brooklyn, NY" in out
    assert 'class="info-card info-card--accent"' in out
    assert ">Saved<" in out


def test_render_transit_fallback_card():
    """Transit coupling: with no weather location, the transit one shows."""
    transit = {
        ls.TRANSIT_LAT_ENV: "40.700",
        ls.TRANSIT_LON_ENV: "-74.000",
        ls.TRANSIT_DISPLAY_NAME_ENV: "Transit Home",
    }
    out = _render(transit_state=transit)
    assert "Transit Home" in out
    assert ">From transit<" in out


def test_render_no_default_state():
    assert "No weather default is set yet." in _render()


def test_render_banner_mirrors_flash_severity():
    # canonical_banner classes by substring: "saved"/"cleared" prefix -> ok,
    # "error"/"fail" anywhere -> danger, otherwise info. These are the exact
    # flash strings the page emits on save/clear, plus a danger-trigger one.
    assert "banner banner--ok" in _render(status_msg="Saved. Voice daemon restarting.")
    assert "banner banner--ok" in _render(status_msg="Cleared weather default. Voice restarting.")
    assert "banner banner--danger" in _render(status_msg="Could not save: write failed")
    # A neutral flash (no save/clear prefix, no error/fail) is info-toned.
    assert "banner banner--info" in _render(status_msg="Enter a location first.")
    assert 'class="banner' not in _render(status_msg="")


# --- public surface ---------------------------------------------------------

def test_public_surface_is_stable():
    assert callable(weather_setup._index_html)
    assert callable(weather_setup.make_server)
    assert callable(weather_setup.main)
    assert callable(weather_setup._make_handler)


# --- routes via a live server (end-to-end, like test_weather_setup.py) ------

@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """Run /weather/ on a random port against tmp state; suppress
    systemctl. Mirrors the other web fixture shapes."""
    monkeypatch.setattr(weather_setup, "restart_voice_daemon", lambda: None)
    state_path = str(tmp_path / "weather.env")
    transit_path = str(tmp_path / "transit.env")

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = weather_setup.make_server(
        ("127.0.0.1", port),
        state_path=state_path,
        transit_path=transit_path,
    )
    base_url = f"http://127.0.0.1:{port}"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield {"url": base_url, "state_path": state_path,
               "transit_path": transit_path}
    finally:
        server.shutdown()
        server.server_close()


def test_get_root_serves_canonical_page(live_server):
    import urllib.request
    body = urllib.request.urlopen(live_server["url"] + "/").read().decode()
    assert "/assets/app.css?v=" in body
    assert 'class="app-header"' in body


def test_get_root_with_tools_return_uses_tool_pack_back_link(live_server):
    import urllib.parse
    import urllib.request
    path = "/?return_to=" + urllib.parse.quote("/tools/pack/weather/", safe="")
    body = urllib.request.urlopen(live_server["url"] + path).read().decode()
    assert 'href="/tools/pack/weather/"' in body


def test_get_root_rejects_off_origin_return_link(live_server):
    import urllib.request
    body = urllib.request.urlopen(
        live_server["url"] + "/?return_to=%2F%2Fevil.test%2F",
    ).read().decode()
    assert 'href="/"' in body
    assert "evil.test" not in body


def test_post_save_manual_coords_writes_and_redirects(live_server):
    post_with_csrf(
        live_server["url"],
        "/save",
        {"manual_lat": "40.700", "manual_lon": "-74.000", "units": "celsius"},
        expect_status=303,
    )
    saved = _common.read_env_file(live_server["state_path"])
    assert saved[weather_setup.LAT_ENV] == "40.700"
    assert saved[weather_setup.LON_ENV] == "-74.000"
    assert saved[weather_setup.UNITS_ENV] == "celsius"


def test_post_clear_redirects(live_server):
    post_with_csrf(live_server["url"], "/clear", {}, expect_status=303)


def test_post_save_bad_units_redirects_with_flash(live_server):
    # Invalid units -> see-other back to ./ carrying an error flash.
    post_with_csrf(
        live_server["url"], "/save", {"units": "kelvin"}, expect_status=303,
    )


def test_unknown_post_path_404s(live_server):
    # A bogus path is route-checked before CSRF, so we still need a session
    # cookie to reach the handler; assert it 404s rather than 403/200.
    post_with_csrf(
        live_server["url"], "/bogus", {}, expect_status=404,
    )


def test_post_save_without_csrf_rejected(live_server):
    """Direct POST with no CSRF cookie/field must 403 (resilience: the
    mutating routes stay protected after the restyle)."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        live_server["url"] + "/save",
        data=b"units=celsius",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 403


def test_get_mints_csrf_cookie(live_server):
    # begin_request()/send_html_response() wiring still mints the cookie.
    session = make_csrf_session(live_server["url"], "/")
    assert session["token"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
