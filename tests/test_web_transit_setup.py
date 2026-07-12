# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /transit/ wizard after its migration to the canonical look.

Companion to tests/test_transit_setup.py (which covers the full state /
save / clear behaviour). This file is the migration guard: it asserts the
page now renders canonical design-system bytes (links /assets/app.css,
carries the shared .app-header + icon sprite + page stylesheet, loads its
behaviour as an ES module) and that the migration was presentation-only —
the routes, the CSRF-protected forms, and the public module surface are
unchanged. Network calls (GBFS, BusTime) are mocked so the suite stays
hardware-free.
"""
from __future__ import annotations

import http
import stat
import urllib.parse

import pytest

from jasper.web import transit_setup

from ._web_test_helpers import FakeHandler

# A 43-char token, the shape secrets.token_urlsafe(32) produces.
TOKEN = "x" * 43


# ---- Coords fixtures ------------------------------------------------------
# Sunset Park is inside the NYC bbox (subway + bus + Citi Bike cards render);
# London is outside every provider's bbox (no-coverage card).
NYC_STATE = {
    "JASPER_TRANSIT_LAT": "40.646",
    "JASPER_TRANSIT_LON": "-73.994",
    "JASPER_TRANSIT_DISPLAY_NAME": "Sunset Park, Brooklyn",
}
LONDON_STATE = {
    "JASPER_TRANSIT_LAT": "51.5",
    "JASPER_TRANSIT_LON": "-0.1",
    "JASPER_TRANSIT_DISPLAY_NAME": "London",
}


@pytest.fixture
def stub_gbfs(monkeypatch):
    """Stub the Citi Bike GBFS fetch so the with-coords render never makes a
    real HTTP call (the card SIRI/GBFS-probes during render)."""
    import jasper.citibike as citibike_mod
    monkeypatch.setattr(
        citibike_mod, "fetch_feed",
        lambda url, ttl, **kw: {"data": {"stations": []}},
    )


def _render(
    state: dict[str, str],
    flash: str = "",
    *,
    routes_state: dict[str, str] | None = None,
    back_href: str = "/",
) -> str:
    return transit_setup._index_html(
        state,
        TOKEN,
        routes_state=routes_state,
        status_msg=flash,
        back_href=back_href,
    ).decode()


# ---- Canonical document shell --------------------------------------------


def test_transit_page_is_canonical_document():
    out = _render({})
    assert out.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in out
    # The old hand-rolled body styling must be gone.
    assert "max-width: 620px" not in out
    assert "1db954" not in out  # no legacy Spotify-green hex


def test_transit_page_links_page_specific_stylesheet():
    out = _render({})
    assert "/assets/transit/transit.css?v=" in out


def test_transit_page_has_shared_app_header():
    out = _render({})
    assert 'class="app-header"' in out
    assert '<h1 class="app-header__title">Transit</h1>' in out
    assert '<use href="#icon-back">' in out


def test_transit_page_honors_safe_back_href():
    out = _render({}, back_href="/tools/pack/nyc-transit/")
    assert 'href="/tools/pack/nyc-transit/"' in out


def test_transit_page_loads_es_module_not_inline_script():
    out = _render(NYC_STATE)
    assert '<script type="module" src="/assets/transit/js/main.js">' in out
    # No inline <script> blocks remain anywhere on the page (the bus /
    # citibike sync handlers + the clear-confirm moved into the module).
    assert "<script>" not in out
    assert "jtsConfirmSubmit" not in out  # legacy inline confirm gone


# ---- Forms keep CSRF + their POST actions ---------------------------------


def test_geocode_form_carries_csrf_field():
    out = _render({})  # cold state shows the address form
    assert 'action="geocode"' in out
    assert 'method="post"' in out
    # csrf_field_html renders a hidden input named csrf_token.
    assert 'name="csrf_token"' in out
    assert 'value="' + TOKEN + '"' in out


def test_save_and_clear_forms_carry_csrf(stub_gbfs):
    out = _render(NYC_STATE)
    assert 'action="save" id="save-form"' in out
    assert 'action="clear" id="clear-form"' in out
    # Both POST forms include the hidden CSRF field.
    assert out.count('name="csrf_token"') >= 2


def test_forms_use_canonical_field_and_button_vocabulary():
    out = _render({})
    assert 'class="field"' in out
    assert 'class="form-actions"' in out
    assert 'class="btn btn--primary"' in out


# ---- Provider cards + conditional logic preserved ------------------------


def test_cold_state_shows_only_address_input():
    out = _render({})
    assert "Where you are" in out
    assert 'name="address"' in out
    # No provider cards before coords.
    assert "provider-card" not in out


def test_with_coords_renders_subway_card_and_locked_bus_card(stub_gbfs):
    out = _render(NYC_STATE)
    assert "Travel time" in out
    assert "NYC Subway" in out
    assert "provider-card" in out
    # Subway picker present (B12 = 9 Av is the nearest hit).
    assert "9 Av" in out
    # Bus card locked: no key set, so only the register CTA + key input.
    assert "needs an API key" in out
    # Locked-card warn badge uses the canonical status token, not a hex.
    assert "--status-warn" in out


def test_travel_routes_card_masks_saved_key_and_selects_default_mode(stub_gbfs):
    state = {**NYC_STATE, "JASPER_TRAVEL_DEFAULT_MODE": "drive"}
    routes = {"GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic-Test_Key"}
    out = _render(state, routes_state=routes)
    assert "Travel time" in out
    assert "configured" in out
    assert "AIza…_Key" in out
    assert "AIzaSySynthetic-Test_Key" not in out
    assert 'value="drive" selected' in out


def test_travel_routes_card_ignores_stale_process_env_key(stub_gbfs, monkeypatch):
    monkeypatch.setenv("GOOGLE_ROUTES_API_KEY", "AIzaSySynthetic-Stale_Key")
    out = _render(NYC_STATE, routes_state={})
    assert "Travel time" in out
    assert "not configured" in out
    assert "AIzaSySynthetic-Stale_Key" not in out
    assert "process environment" not in out


def test_with_coords_renders_citibike_ebike_toggle(stub_gbfs):
    out = _render(NYC_STATE)
    assert "Citi Bike" in out
    assert "Only mention e-bikes" in out
    # The household toggle uses the canonical .toggle markup contract, posted
    # with the save form. The literal class="switch" must NOT appear (gating).
    assert 'class="toggle"' in out
    assert 'name="citibike_ebike_only" form="save-form"' in out
    assert 'class="switch"' not in out
    # Hidden marker field always emitted (the "card was rendered" sentinel).
    assert 'name="citibike_stations"' in out


def test_ebike_only_checkbox_checked_when_set(monkeypatch):
    import jasper.citibike as citibike_mod
    monkeypatch.setattr(
        citibike_mod, "fetch_feed",
        lambda url, ttl, **kw: {"data": {"stations": []}},
    )
    state = dict(NYC_STATE, JASPER_CITIBIKE_EBIKE_ONLY="1")
    out = _render(state)
    assert 'name="citibike_ebike_only" form="save-form" checked' in out


def test_subway_direction_defaults_to_both_when_unset(stub_gbfs):
    """Round-trip safety carried over from the legacy page: an unconfigured
    direction renders 'both' selected, not 'uptown'."""
    out = _render(NYC_STATE)
    assert 'value="both" selected' in out


def test_outside_coverage_shows_no_coverage_card():
    out = _render(LONDON_STATE)
    assert "Travel time" in out
    assert 'id="save-form"' in out
    assert "No transit support" in out
    assert "no-coverage" in out
    # No subway/bus card for an unsupported area.
    assert "NYC Subway" not in out


def test_advanced_section_present_in_every_state(stub_gbfs):
    for state in ({}, NYC_STATE, LONDON_STATE):
        out = _render(state)
        assert 'class="advanced"' in out
        # Manual lat/lon override form still posts to geocode.
        assert 'name="manual_lat"' in out


def test_flash_renders_canonical_banner():
    # canonical_banner() classes by message text: "saved"/"cleared" prefix
    # -> ok, a message containing "error"/"fail" -> danger, anything else
    # -> info. The flash strings the wizard writes hit those buckets.
    assert "banner--ok" in _render({}, flash="Saved. Voice daemon restarting.")
    assert "banner--ok" in _render({}, flash="Cleared transit settings. Voice restarting.")
    assert "banner--danger" in _render({}, flash="MTA BusTime rejected that key (probe failed).")
    # A neutral flash (e.g. the geocode "Found location: …") -> info banner.
    assert "banner--info" in _render({}, flash="Found location: Sunset Park")
    # No flash → no banner element.
    assert 'class="banner' not in _render({})


# ---- Public surface + routes (presentation-only migration) ----------------


def _handler_cls(tmp_path):
    return transit_setup._make_handler({
        "state_path": str(tmp_path / "transit.env"),
        "routes_secret_path": str(tmp_path / "google_routes.env"),
        "weather_path": str(tmp_path / "weather.env"),
    })


def _bound_handler(tmp_path, fake: FakeHandler):
    """A real closure-Handler instance carrying the fake's request attributes.

    `do_POST` delegates to sibling methods (`self._handle_clear()` etc.) that
    only exist on the closure `Handler` class, so a bare `FakeHandler` can't
    drive those branches. Construct the real `Handler` via `__new__` (skipping
    BaseHTTPRequestHandler.__init__, which would touch a socket) and graft the
    fake's request/response stand-ins onto it — the instance keeps the real
    `_handle_*` methods + the closed-over `cfg`."""
    cls = _handler_cls(tmp_path)
    inst = cls.__new__(cls)
    inst.__dict__.update(fake.__dict__)
    # Response/IO shims live on the fake's class, not its instance dict, so
    # bind them through explicitly.
    for name in (
        "send_response", "send_header", "end_headers", "send_error",
        "address_string", "log_message", "header_values",
    ):
        setattr(inst, name, getattr(fake, name))
    return inst


def test_public_surface_is_stable():
    assert callable(transit_setup.make_server)
    assert callable(transit_setup.main)
    assert callable(transit_setup._index_html)


def test_get_root_renders_canonical_page(tmp_path):
    handler = _handler_cls(tmp_path)
    h = FakeHandler("/")
    handler.do_GET(h)
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert "/assets/app.css?v=" in out
    assert 'class="app-header"' in out


def test_get_root_with_tools_return_uses_tool_pack_back_link(tmp_path):
    handler = _handler_cls(tmp_path)
    h = FakeHandler("/?return_to=%2Ftools%2Fpack%2Fnyc-transit%2F")
    handler.do_GET(h)
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert 'href="/tools/pack/nyc-transit/"' in out


def test_get_root_rejects_off_origin_return_link(tmp_path):
    handler = _handler_cls(tmp_path)
    h = FakeHandler("/?return_to=%2F%2Fevil.test%2F")
    handler.do_GET(h)
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert 'href="/"' in out
    assert "evil.test" not in out


def test_post_unknown_route_404s(tmp_path):
    handler = _handler_cls(tmp_path)
    h = FakeHandler("/nope", body=b"")
    handler.do_POST(h)
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_post_clear_with_csrf_redirects_and_restarts(tmp_path, monkeypatch):
    """The clear route still verifies CSRF, restarts voice, and 303s — the
    confirm moved client-side but the server contract is unchanged."""
    restarts: list[None] = []
    monkeypatch.setattr(
        transit_setup, "restart_voice_daemon", lambda: restarts.append(None),
    )
    token = "z" * 64
    # csrf_token = form field (_common.CSRF_FORM_FIELD); jts_csrf = cookie.
    body = ("csrf_token=" + token).encode()
    h = FakeHandler("/clear", body=body, cookies="jts_csrf=" + token)
    _bound_handler(tmp_path, h).do_POST()
    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert h.header_values("Location") == ["./"]
    assert restarts == [None]


def test_post_save_writes_routes_key_to_secret_file_and_default_to_transit_env(
    tmp_path,
    monkeypatch,
):
    restarts: list[None] = []
    monkeypatch.setattr(
        transit_setup, "restart_voice_daemon", lambda: restarts.append(None),
    )
    token = "z" * 64
    key = "AIzaSySynthetic-Test_Key"
    body = urllib.parse.urlencode({
        "csrf_token": token,
        "travel_default_mode": "drive",
        "google_routes_key": key,
    }).encode()
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + token)
    _bound_handler(tmp_path, h).do_POST()

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert restarts == [None]
    transit_state = transit_setup._load_state(str(tmp_path / "transit.env"))
    routes_state = transit_setup.read_env_file(str(tmp_path / "google_routes.env"))
    assert transit_state["JASPER_TRAVEL_DEFAULT_MODE"] == "drive"
    assert "GOOGLE_ROUTES_API_KEY" not in transit_state
    assert routes_state == {"GOOGLE_ROUTES_API_KEY": key}
    mode = stat.S_IMODE((tmp_path / "google_routes.env").stat().st_mode)
    assert mode == transit_setup.SECRET_ENV_MODE


def test_post_save_blank_routes_key_preserves_existing_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(transit_setup, "restart_voice_daemon", lambda: None)
    transit_setup.write_env_file(
        str(tmp_path / "google_routes.env"),
        {"GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic-Keep_Key"},
        mode=transit_setup.SECRET_ENV_MODE,
    )
    token = "z" * 64
    body = urllib.parse.urlencode({
        "csrf_token": token,
        "travel_default_mode": "transit",
        "google_routes_key": "",
    }).encode()
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + token)
    _bound_handler(tmp_path, h).do_POST()

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    routes_state = transit_setup.read_env_file(str(tmp_path / "google_routes.env"))
    assert routes_state["GOOGLE_ROUTES_API_KEY"] == "AIzaSySynthetic-Keep_Key"


def test_post_clear_removes_routes_secret_file(tmp_path, monkeypatch):
    monkeypatch.setattr(transit_setup, "restart_voice_daemon", lambda: None)
    transit_setup.write_env_file(
        str(tmp_path / "google_routes.env"),
        {"GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic-Clear_Key"},
        mode=transit_setup.SECRET_ENV_MODE,
    )
    token = "z" * 64
    body = ("csrf_token=" + token).encode()
    h = FakeHandler("/clear", body=body, cookies="jts_csrf=" + token)
    _bound_handler(tmp_path, h).do_POST()

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert not (tmp_path / "google_routes.env").exists()


def test_post_clear_rejects_bad_csrf(tmp_path):
    # Form-field token differs from the cookie token → 403, no restart.
    body = b"csrf_token=" + b"a" * 64
    h = FakeHandler("/clear", body=body, cookies="jts_csrf=" + "b" * 64)
    _bound_handler(tmp_path, h).do_POST()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)


# ---- City-pack toggle (PR2) -----------------------------------------------


def test_cities_section_renders_with_nyc_toggle_on_by_default(stub_gbfs):
    # Unset JASPER_TRANSIT_CITIES => all packs enabled (legacy default), so
    # the NYC toggle renders checked and the provider cards show.
    out = _render(NYC_STATE)
    assert 'id="cities-form"' in out
    assert 'name="city_nyc"' in out
    # The NYC toggle is checked when the pack is enabled.
    assert "checked" in out.split('name="city_nyc"')[1][:40]
    assert "covers your location" in out
    assert 'id="save-form"' in out  # provider cards render for the enabled city


def test_cities_toggle_off_gates_provider_cards_and_shows_nudge():
    # NYC present but turned off (explicit empty value => no packs).
    state = {**NYC_STATE, "JASPER_TRANSIT_CITIES": ""}
    out = _render(state)
    assert 'id="cities-form"' in out
    # Toggle renders UNchecked, and the geocode-driven "available here" nudge
    # invites turning it on.
    assert "checked" not in out.split('name="city_nyc"')[1][:40]
    assert "available at your location" in out
    # With the only covering city off, local provider cards are gated out, but
    # the global Google Routes card remains configurable.
    assert "Travel time" in out
    assert "NYC Subway" not in out
    assert 'id="save-form"' in out


def test_cities_section_carries_csrf(stub_gbfs):
    out = _render(NYC_STATE)
    form = out.split('id="cities-form"')[1].split("</form>")[0]
    assert "csrf_token" in form


def test_post_cities_enables_pack_writes_env_and_restarts(tmp_path, monkeypatch):
    restarts: list[None] = []
    monkeypatch.setattr(
        transit_setup, "restart_voice_daemon", lambda: restarts.append(None),
    )
    # Seed coords so the round-trip preserves them alongside the new toggle.
    transit_setup.write_env_file(
        str(tmp_path / "transit.env"), dict(NYC_STATE), mode=0o640,
    )
    token = "z" * 64
    body = ("csrf_token=" + token + "&city_nyc=on").encode()
    h = FakeHandler("/cities", body=body, cookies="jts_csrf=" + token)
    _bound_handler(tmp_path, h).do_POST()
    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert restarts == [None]
    saved = transit_setup._load_state(str(tmp_path / "transit.env"))
    assert saved["JASPER_TRANSIT_CITIES"] == "nyc"
    assert saved["JASPER_TRANSIT_LAT"] == NYC_STATE["JASPER_TRANSIT_LAT"]  # coords kept


def test_post_cities_uncheck_all_writes_empty_value(tmp_path, monkeypatch):
    # Unchecking every city must persist an EXPLICIT empty value (present, not
    # absent) so enabled_pack_ids reads it as "no cities" rather than falling
    # back to the absent-key "all" default. This is the toggle's whole point.
    monkeypatch.setattr(transit_setup, "restart_voice_daemon", lambda: None)
    transit_setup.write_env_file(
        str(tmp_path / "transit.env"),
        {**NYC_STATE, "JASPER_TRANSIT_CITIES": "nyc"},
        mode=0o640,
    )
    token = "z" * 64
    body = ("csrf_token=" + token).encode()  # no city_* fields => all off
    h = FakeHandler("/cities", body=body, cookies="jts_csrf=" + token)
    _bound_handler(tmp_path, h).do_POST()
    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    saved = transit_setup._load_state(str(tmp_path / "transit.env"))
    assert saved["JASPER_TRANSIT_CITIES"] == ""
    # And that empty value resolves to zero enabled packs.
    from jasper import transit
    assert transit.enabled_pack_ids(saved) == ()


def test_post_cities_rejects_bad_csrf(tmp_path, monkeypatch):
    restarts: list[None] = []
    monkeypatch.setattr(
        transit_setup, "restart_voice_daemon", lambda: restarts.append(None),
    )
    body = b"csrf_token=" + b"a" * 64 + b"&city_nyc=on"
    h = FakeHandler("/cities", body=body, cookies="jts_csrf=" + "b" * 64)
    _bound_handler(tmp_path, h).do_POST()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)
    assert restarts == []


# ---- Secret scrubbing on broad-except paths (DA-0035) ---------------------
# An unanticipated (non-TransitError) exception whose repr embeds the live
# BusTime key in a ?key=... URL must be scrubbed before it reaches either the
# journal OR the html.escape()-d error banner served on the household LAN.

from types import SimpleNamespace

_SECRET_KEY = "SUPERSECRETBUSKEY123"
_LEAKY_URL = f"https://bustime.mta.info/api/siri?key={_SECRET_KEY}&stop=1"


def _fake_bus_provider(exc: Exception) -> SimpleNamespace:
    def _raise(*a, **k):
        raise exc

    return SimpleNamespace(
        label="MTA Bus",
        credentials=[SimpleNamespace(label="BusTime key", placeholder="key", help_url="http://x")],
        find_stops_near=_raise,
        validate_credentials=_raise,
    )


def test_bus_card_scrubs_key_from_error_banner():
    provider = _fake_bus_provider(RuntimeError(f"boom {_LEAKY_URL}"))
    state = {**NYC_STATE, "JASPER_MTA_BUSTIME_KEY": _SECRET_KEY}
    html_out = transit_setup._bus_card_html(provider, state)
    assert _SECRET_KEY not in html_out
    assert "key=***" in html_out


def test_bus_card_scrubs_key_from_log(caplog):
    provider = _fake_bus_provider(RuntimeError(f"boom {_LEAKY_URL}"))
    state = {**NYC_STATE, "JASPER_MTA_BUSTIME_KEY": _SECRET_KEY}
    with caplog.at_level("WARNING"):
        transit_setup._bus_card_html(provider, state)
    assert _SECRET_KEY not in caplog.text
    assert "bus stops fetch raised" in caplog.text


def test_apply_save_scrubs_key_from_probe_log(caplog):
    provider = _fake_bus_provider(RuntimeError(f"boom {_LEAKY_URL}"))
    form = {"nyc_bus_key": _SECRET_KEY}
    with caplog.at_level("WARNING"):
        _current, error = transit_setup._apply_save(form, {}, bus_provider=provider)
    assert _SECRET_KEY not in caplog.text
    # The user-facing error is a generic "probe failed", never the raw key.
    assert error and _SECRET_KEY not in error


def test_citibike_card_scrubs_url_from_error_banner(monkeypatch):
    # Keyless, but mirrors the bus scrub discipline: any URL in an error
    # repr is masked in the banner.
    def _raise(*a, **k):
        raise RuntimeError(f"boom {_LEAKY_URL}")

    provider = SimpleNamespace(
        label="Citi Bike",
        credentials=[],
        find_stops_near=_raise,
    )
    html_out = transit_setup._citibike_card_html(provider, NYC_STATE)
    assert _SECRET_KEY not in html_out
    assert "key=***" in html_out
