# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Transit configuration wizard at /transit/.

UX (single page):

  1. Address — user types a free-form address. Server geocodes via
     OSM Nominatim (Photon fallback) and stores coordinates in
     `/var/lib/jasper/transit.env`. Only coords land on disk; the
     address itself never persists.

  2. Cities — one on/off toggle per `jasper.transit.CityPack` that
     covers the user's coords (today just New York City). Writes
     `JASPER_TRANSIT_CITIES` and is the master switch for a city's
     transit: the provider cards below render only for enabled cities.
     A covering-but-off city shows an "available here" nudge — the
     geocode-driven suggestion to turn the detected city on.

  3. One card per provider whose pack is ENABLED and whose bounding box
     covers the user's coords. The subway card is keyless and renders
     nearest stops immediately. The bus card is locked until the user
     pastes a BusTime API key — that's a hard prerequisite (the
     stops-lookup endpoint itself requires a key), so the locked card
     shows ONLY a register link + key input.

  4. Advanced — collapsed `<details>` with raw stop-ID / line / route
     inputs for power users and recovery from a misconfigured save.

Persistence: all transit env vars live in `/var/lib/jasper/transit.env`
at mode 0640. The systemd unit for jasper-voice sources this file
AFTER `/etc/jasper/jasper.env`, so wizard-written values win — same
pattern as `voice_provider.env` and `wake_model.env`.

Modularity: the page is data-driven by `jasper.transit.REGISTRY`. To
add a new provider (Berlin BVG, Citi Bike, ...), drop a module under
`jasper.transit.providers.` and append it to the REGISTRY tuple. The
wizard auto-renders a card for it when the user's coords fall in its
bounding box. Provider-specific config (subway's direction radio,
bus's routes checkboxes) is dispatched on `provider.id` — extend the
dispatch when a third provider needs its own knob.

Restart: every successful save kicks `systemctl restart jasper-voice`
(non-blocking, see `_common.restart_voice_daemon`). The transit tools
re-register on the daemon's next boot based on the new env values.

URL surface (after nginx strips /transit/):
  GET  /             page render (geocodes once on Submit, never on render)
  POST /geocode      address → coords; redirects back
  POST /save         persist picks; restart voice; redirects back
  POST /cities       persist city-pack on/off toggles; restart voice
  POST /clear        wipe transit config; restart voice; redirects back
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import re
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from concurrent.futures import ThreadPoolExecutor

from .. import google_routes, location_state, transit
from ..bus import parse_bus_stops
from ..transit import geocode as geocode_mod
from ..log_event import log_event
from ._common import (
    begin_request,
    canonical_banner,
    canonical_header,
    canonical_page,
    csrf_field_html,
    delete_env_file,
    read_env_file,
    reject_csrf,
    send_html_response,
    send_see_other,
    guard_read_request,
    guard_mutating_request,
    read_form,
    restart_voice_daemon,
    safe_back_href,
    SECRET_ENV_MODE,
    mask_secret,
    write_env_file,
)

logger = logging.getLogger(__name__)


# Persisted at /var/lib/jasper/transit.env. Mode 0640 — the BusTime
# key is mildly sensitive but not as critical as an OAuth token.
TRANSIT_FILE = location_state.TRANSIT_FILE
TRANSIT_FILE_MODE = location_state.TRANSIT_FILE_MODE
GOOGLE_ROUTES_SECRET_FILE = google_routes.GOOGLE_ROUTES_SECRET_FILE

# Wizard-owned coordinate state. Provider-owned env keys come from
# `transit.all_env_keys()`. Splitting these is deliberate: coords
# are wizard-internal scaffolding, not consumed by daemons directly.
LAT_ENV = location_state.TRANSIT_LAT_ENV
LON_ENV = location_state.TRANSIT_LON_ENV
DISPLAY_NAME_ENV = location_state.TRANSIT_DISPLAY_NAME_ENV
TRAVEL_DEFAULT_MODE_ENV = google_routes.TRAVEL_DEFAULT_MODE_ENV
GOOGLE_ROUTES_API_KEY_ENV = google_routes.GOOGLE_ROUTES_API_KEY_ENV

_KEY_VALID_RE = re.compile(r"^[A-Za-z0-9_\-.~]+$")

# Max distance (mi) to a nearest stop before we consider the provider
# uncovered. NYC bbox includes some areas (e.g., Sandy Hook NJ tip)
# that are technically in the rectangle but where the nearest subway
# is 10+ mi away — treat those as no-coverage to spare the user a
# misleading card.
MAX_NEAREST_STOP_MILES = 5.0


# ----------------------------------------------------------------------
# State helpers — pure functions, IO confined to read/write_env_file.
# ----------------------------------------------------------------------


def _owned_env_keys() -> set[str]:
    """Every env key this wizard writes. Used to filter the env file
    on save so foreign keys an operator placed in transit.env (rare)
    survive unchanged."""
    return {
        LAT_ENV, LON_ENV, DISPLAY_NAME_ENV,
        TRAVEL_DEFAULT_MODE_ENV,
        transit.TRANSIT_CITIES_ENV,  # city-pack on/off toggle
        *transit.all_env_keys(),
    }


def _load_state(path: str = TRANSIT_FILE) -> dict[str, str]:
    return read_env_file(path)


def _seed_weather_from_transit_if_missing(
    transit_state: dict[str, str],
    *,
    weather_path: str = location_state.WEATHER_FILE,
) -> bool:
    """Copy transit coords into weather.env when weather has no coords.

    Weather and transit stay independent after this seed. Existing
    weather coordinates win so a household can keep different values.
    """
    loc = location_state.parse_transit_location(transit_state)
    if loc is None:
        return False
    weather_state = read_env_file(weather_path)
    if location_state.parse_weather_location(weather_state) is not None:
        return False
    units = (
        weather_state.get(location_state.WEATHER_UNITS_ENV, "").strip()
        or os.environ.get(location_state.WEATHER_UNITS_ENV, "").strip()
        or None
    )
    new_weather = dict(weather_state)
    new_weather.update(location_state.weather_env_for_location(loc, units=units))
    write_env_file(
        weather_path,
        new_weather,
        mode=location_state.WEATHER_FILE_MODE,
    )
    return True


def _value_for(state: dict[str, str], env_var: str, default: str = "") -> str:
    """State value, falling back to the process env (operator may have
    set the var in /etc/jasper/jasper.env), then to default. Mirrors
    voice_setup._value_for so the wizard always shows the value the
    daemon would actually use."""
    val = state.get(env_var, "").strip()
    if val:
        return val
    return os.environ.get(env_var, "") or default


def _coords(state: dict[str, str]) -> tuple[float, float] | None:
    """Parsed (lat, lon) or None if not geocoded yet."""
    try:
        return (
            float(_value_for(state, LAT_ENV)),
            float(_value_for(state, LON_ENV)),
        )
    except ValueError:
        return None


def _has_bus_key(state: dict[str, str]) -> bool:
    """True iff a BusTime key is persisted in the wizard's state file.

    Intentionally does NOT consult `os.environ`. The wizard process
    is long-lived (10-min idle); `os.environ` is captured at process
    start and won't reflect a post-startup migration that moved the
    key out of jasper.env. Consulting the persisted state directly
    keeps render decisions and save decisions consistent."""
    return bool(state.get("JASPER_MTA_BUSTIME_KEY", "").strip())


def _bus_key_source(state: dict[str, str]) -> str:
    """Return where the bus key lives: 'state' / 'env' / 'none'.

    'state' = persisted in /var/lib/jasper/transit.env (the wizard's
              owned file; what `_has_bus_key` looks at).
    'env'   = visible in os.environ (operator pasted it into
              /etc/jasper/jasper.env directly, OR migrated by
              install.sh into transit.env which systemd re-sourced
              into our env on the next jasper-web spawn).
    'none'  = not set anywhere.

    Used to drive the locked / soft-unlocked / unlocked card states.
    Save decisions still hinge on `_has_bus_key` (state-only) — this
    function is for rendering only."""
    if state.get("JASPER_MTA_BUSTIME_KEY", "").strip():
        return "state"
    if os.environ.get("JASPER_MTA_BUSTIME_KEY", "").strip():
        return "env"
    return "none"


def _routes_key_source(routes_state: dict[str, str]) -> str:
    """Return where the wizard-owned Google Routes key lives."""
    if routes_state.get(GOOGLE_ROUTES_API_KEY_ENV, "").strip():
        return "state"
    return "none"


def _routes_key_value(routes_state: dict[str, str]) -> str:
    return routes_state.get(GOOGLE_ROUTES_API_KEY_ENV, "").strip()


def _validate_google_routes_key(key: str) -> str | None:
    if not key:
        return None
    if any(ch.isspace() for ch in key):
        return "Google Routes API key contains whitespace; copy it again."
    if not _KEY_VALID_RE.fullmatch(key):
        return (
            "Google Routes API key contains characters that don't look like "
            "an API key; copy it again."
        )
    return None


def _mask_key(value: str) -> str:
    """Render a BusTime key as `prefix…suffix` for display. Empty input
    returns empty string. Mirrors `_common.mask_secret` but inlined
    here so the bus card can show a value sourced from os.environ
    (which `_common` doesn't know about)."""
    value = value.strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "…" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def _badge_html(configured: bool) -> str:
    """A canonical status badge for a provider card's header.

    One knob: ``--tone`` selects the accent the ``.badge`` primitive reads,
    so a configured card greens (status-ok) and an unconfigured one stays
    neutral (status-idle) — matching the rest of the design system."""
    if configured:
        return '<span class="badge" style="--tone:var(--status-ok)">configured</span>'
    return '<span class="badge" style="--tone:var(--status-idle)">not configured</span>'


# ----------------------------------------------------------------------
# Save logic — pure where possible.
# ----------------------------------------------------------------------


def _apply_geocode(
    form: dict[str, str], current: dict[str, str],
) -> tuple[dict[str, str], str | None]:
    """Geocode the submitted address and return updated state.

    Empty input means "use the manual lat/lon fields instead" — the
    Advanced section ships those, and they bypass Nominatim entirely
    (privacy-maximalist path)."""
    address = (form.get("address") or "").strip()
    manual_lat = (form.get("manual_lat") or "").strip()
    manual_lon = (form.get("manual_lon") or "").strip()

    new = dict(current)

    if manual_lat or manual_lon:
        if not (manual_lat and manual_lon):
            return current, "Enter both latitude and longitude, or use the address field above."
        try:
            lat = geocode_mod.round_coord(float(manual_lat))
            lon = geocode_mod.round_coord(float(manual_lon))
        except ValueError:
            return current, "Latitude and longitude must be numbers."
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return current, "Latitude must be -90..90 and longitude -180..180."
        new[LAT_ENV] = f"{lat:.3f}"
        new[LON_ENV] = f"{lon:.3f}"
        new[DISPLAY_NAME_ENV] = f"Manual: {lat:.3f}, {lon:.3f}"
        return new, None

    if not address:
        return current, "Enter an address."
    try:
        result = geocode_mod.geocode(address)
    except geocode_mod.GeocodeError as e:
        return current, f"Couldn't geocode that — {e}"
    new[LAT_ENV] = f"{geocode_mod.round_coord(result.lat):.3f}"
    new[LON_ENV] = f"{geocode_mod.round_coord(result.lon):.3f}"
    new[DISPLAY_NAME_ENV] = result.display_name
    return new, None


def _apply_save(
    form: dict[str, str], current: dict[str, str],
    *,
    bus_provider: transit.TransitProvider | None = None,
) -> tuple[dict[str, str], str | None]:
    """Apply submitted picks to state. Validates BusTime key if pasted.

    `bus_provider` is parameterised so tests can swap a stub; production
    callers default to the registry's bus provider.
    """
    if bus_provider is None:
        bus_provider = transit.by_id("nyc_bus")

    new = dict(current)

    # Google Routes default mode. This is intentionally an overall travel
    # mode, not a transit-subtype preference; the voice tool maps explicit
    # user wording per call.
    if "travel_default_mode" in form:
        raw_mode = (form.get("travel_default_mode") or "").strip()
        mode = google_routes.normalize_travel_mode(raw_mode)
        if mode not in google_routes.TRAVEL_MODE_TO_API:
            return current, (
                "Default travel mode must be transit, drive, walk, or bicycle."
            )
        new[TRAVEL_DEFAULT_MODE_ENV] = mode

    # Subway picks. Empty values mean "leave alone" — don't drop saved
    # config just because the user re-saved after editing only bus.
    sub_stop = (form.get("nyc_subway_stop") or "").strip()
    if sub_stop:
        new["JASPER_SUBWAY_STATION_ID"] = sub_stop
    sub_dir = (form.get("nyc_subway_direction") or "").strip().lower()
    # `both` explicitly drops the env var (daemon prompts each time).
    # uptown/downtown sets the explicit default. Anything else (typo,
    # absent field on a partial form) leaves state unchanged so a
    # bus-only save doesn't reset the subway direction.
    if sub_dir == "both":
        new.pop("JASPER_SUBWAY_DEFAULT_DIRECTION", None)
    elif sub_dir in ("uptown", "downtown"):
        new["JASPER_SUBWAY_DEFAULT_DIRECTION"] = sub_dir

    # Bus key — pasted means replace; blank means keep. The lookup
    # endpoint requires a key, so we validate on paste.
    new_key = (form.get("nyc_bus_key") or "").strip()
    if new_key:
        if bus_provider is None:
            return current, "Bus provider unavailable."
        try:
            errors = bus_provider.validate_credentials(
                {"JASPER_MTA_BUSTIME_KEY": new_key},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("bus credential probe raised: %r", e)
            errors = {"JASPER_MTA_BUSTIME_KEY": "probe failed"}
        if errors:
            return current, (
                "MTA BusTime rejected that key. Double-check you copied it "
                "correctly, or wait a few minutes — fresh keys can take ~30 "
                "minutes to activate."
            )
        new["JASPER_MTA_BUSTIME_KEY"] = new_key

    # Bus stop picks (multi). Defensive against the locked-card POST
    # bypass: if no key is in `new` (either from this form or already
    # persisted), refuse to write bus picks. A crafted POST that
    # submits picks without a key would otherwise persist stop IDs
    # the daemon can't use — the runtime SIRI client also needs the
    # key and would fail silently at every voice query.
    if new.get("JASPER_MTA_BUSTIME_KEY", "").strip():
        # The form ships `nyc_bus_stop` as a list (multi-checkbox).
        # _common.read_form collapses duplicates to the last value
        # by default, so the wizard sends a separate hidden field
        # `nyc_bus_stops` carrying the full comma-joined selection.
        # See _bus_card_html for the contract.
        picks_raw = (form.get("nyc_bus_stops") or "").strip()
        if picks_raw:
            new["JASPER_BUS_STOPS"] = picks_raw
        elif "nyc_bus_stops" in form:
            # Explicit empty submission (every checkbox unchecked)
            # → drop the saved list.
            new.pop("JASPER_BUS_STOPS", None)

    # Citi Bike picks + ebike-only toggle. The hidden citibike_stations
    # field's presence in the form is the "card was rendered" marker —
    # absent means the user is out of coverage and we must not touch
    # citibike state. Present (even empty) means the card was shown
    # and the user's submission is authoritative.
    if "citibike_stations" in form:
        picks_raw = form["citibike_stations"].strip()
        if picks_raw:
            new["JASPER_CITIBIKE_STATIONS"] = picks_raw
        else:
            # Empty submission → drop saved stations entirely.
            new.pop("JASPER_CITIBIKE_STATIONS", None)
        # Checkbox is absent from form when unchecked (HTML form
        # semantics). Presence implies checked.
        if form.get("citibike_ebike_only", "").strip():
            new["JASPER_CITIBIKE_EBIKE_ONLY"] = "1"
        else:
            new.pop("JASPER_CITIBIKE_EBIKE_ONLY", None)

    return new, None


def _apply_routes_save(
    form: dict[str, str], current: dict[str, str],
) -> tuple[dict[str, str], str | None]:
    """Apply the Google Routes secret fields.

    Blank key input preserves the existing saved key. The clear checkbox drops
    it. Structural validation only — no billable API probe on save.
    """
    new = dict(current)
    if form.get("google_routes_clear_key", "").strip():
        new.pop(GOOGLE_ROUTES_API_KEY_ENV, None)
        return new, None
    key = (form.get("google_routes_key") or "").strip()
    if not key:
        return new, None
    err = _validate_google_routes_key(key)
    if err is not None:
        return current, err
    new[GOOGLE_ROUTES_API_KEY_ENV] = key
    return new, None


def _apply_clear(current: dict[str, str]) -> dict[str, str]:
    """Drop every wizard-owned key, then record JASPER_TRANSIT_CITIES="" so
    "Clear all transit settings" means transit is OFF. Foreign keys survive.

    The explicit empty value is the one owned key Clear writes rather than
    drops. An ABSENT key reads as "all packs eligible" (the fresh-install
    default), so after an explicit Clear every city would show "enabled" on
    /state and the dashboard with nothing configured — misleading today, and a
    real wrong-state once a second city ships ("Clear all" → both cities ON).
    Present-empty reads as "no cities", so post-Clear the wizard shows each
    covering city as "available — turn on" and /state reports them disabled,
    matching the user's intent. (Re-enabling a city is then an explicit step
    when reconfiguring — consistent with having cleared the toggle.)"""
    kept = {
        k: v for k, v in current.items()
        if k not in _owned_env_keys()
    }
    kept[transit.TRANSIT_CITIES_ENV] = ""
    return kept


def _apply_cities(
    form: dict[str, str], current: dict[str, str],
) -> dict[str, str]:
    """Apply the city-pack on/off toggles to state.

    Each pack renders a checkbox named ``city_<pack.id>``; HTML form
    semantics omit an unchecked checkbox, so a present field means "on". We
    write the *explicit* comma-separated enabled list to
    ``JASPER_TRANSIT_CITIES`` — always explicit (the wizard owns the value),
    and an empty string when nothing is checked. ``enabled_pack_ids`` reads a
    present-but-empty value as "no cities" (distinct from an absent key,
    which is the legacy "all" default), so unchecking everything genuinely
    turns transit off rather than silently re-enabling all packs.
    """
    new = dict(current)
    enabled = [
        pack.id for pack in transit.CITY_PACKS
        if form.get(f"city_{pack.id}", "").strip()
    ]
    new[transit.TRANSIT_CITIES_ENV] = ",".join(enabled)
    return new


# ----------------------------------------------------------------------
# Page rendering.
# ----------------------------------------------------------------------


# Page-specific CSS for the picker rows, cluster headings, locked-bus card,
# geocode result panel, and no-coverage card lives in the static stylesheet
# served from /assets/ (cache-busted by build SHA via canonical_page). Shared
# primitives (cards, fields, buttons, badges, banner, toggle) come from
# app.css.
TRANSIT_CSS_HREF = "/assets/transit/transit.css"


def _wrap_transit_page(
    title: str,
    body_main: str,
    *,
    status_msg: str = "",
    back_href: str = "/",
) -> bytes:
    """Assemble the canonical document shell around the page's <main> content.

    ``body_main`` is the inner HTML of ``<main class="page">`` (everything
    below the sticky header and the flash banner). This helper prepends the
    canonical header + banner, wraps the content in ``<main>``, appends the
    page's ES module, and hands the lot to ``canonical_page`` so the shared
    stylesheet, CSRF meta tag, and icon sprite are emitted once."""
    body = (
        canonical_header(title, back_href=back_href)
        + '\n<main class="page">\n'
        + canonical_banner(status_msg)
        + body_main
        + '\n</main>\n'
        + '<script type="module" src="/assets/transit/js/main.js"></script>'
    )
    # csrf_token is woven into the forms via csrf_field_html already; the meta
    # tag is unused by this page's module (it posts via real forms, not fetch)
    # but canonical_page only emits it when a token is passed, so pass "" and
    # rely on the hidden form fields for CSRF. Page CSS rides as a real,
    # lintable static file (page_css_href), the preferred canonical form.
    return canonical_page(
        title, body, page_css_href=TRANSIT_CSS_HREF,
    )


def _address_section_html(state: dict[str, str], csrf_token: str) -> str:
    coords = _coords(state)
    display = _value_for(state, DISPLAY_NAME_ENV)
    csrf = csrf_field_html(csrf_token)

    if coords is not None:
        lat, lon = coords
        # "Found you here" panel with a Re-geocode form, revealed by the
        # module's Change button (data-action="change-address").
        return f"""
<p class="eyebrow">Where you are</p>
<div class="address-result" id="address-result">
  <div class="label">
    <strong>{html.escape(display) or "(saved location)"}</strong>
    <span class="coords">{lat:.3f}, {lon:.3f} (~110&nbsp;m precision)</span>
  </div>
  <button type="button" class="btn btn--ghost" data-action="change-address">Change…</button>
</div>
<form method="post" action="geocode" id="redo-form" hidden>
  {csrf}
  <div class="field">
    <label for="address-redo">New address</label>
    <input id="address-redo" name="address" type="text"
           placeholder="123 Main St, Brooklyn NY"
           autocomplete="street-address">
    <p class="form-hint">
      Your address is sent to <a href="https://nominatim.openstreetmap.org/" target="_blank" rel="noopener">OpenStreetMap (Nominatim)</a>
      to look up coordinates. Only the coordinates (rounded to ~110&nbsp;m) are saved on this speaker.
      <a href="https://operations.osmfoundation.org/policies/nominatim/" target="_blank" rel="noopener">Policy ↗</a>
    </p>
  </div>
  <div class="form-actions">
    <button type="submit" class="btn btn--primary">Find nearby stops</button>
  </div>
</form>"""

    # Cold state — no coords yet. Big address input as the only thing
    # the user can do.
    return f"""
<p class="eyebrow">Where you are</p>
<p class="form-hint">Enter your home address. We'll use it to find nearby transit stops.</p>
<form method="post" action="geocode">
  {csrf}
  <div class="field">
    <label for="address">Home address</label>
    <input id="address" name="address" type="text"
           placeholder="123 Main St, Brooklyn NY"
           autocomplete="street-address" autofocus>
    <p class="form-hint">
      Your address is sent to <a href="https://nominatim.openstreetmap.org/" target="_blank" rel="noopener">OpenStreetMap (Nominatim)</a>
      to look up coordinates. Only the coordinates (rounded to ~110&nbsp;m) are saved on this speaker — never the address itself.
      <a href="https://operations.osmfoundation.org/policies/nominatim/" target="_blank" rel="noopener">Policy ↗</a>
    </p>
  </div>
  <div class="form-actions">
    <button type="submit" class="btn btn--primary">Find nearby stops</button>
  </div>
</form>"""


def _stop_picker_rows_html(
    *,
    radio_name: str,
    stops: list[transit.Stop],
    active_id: str,
) -> str:
    rows: list[str] = []
    for s in stops:
        is_active = s.stop_id == active_id
        cls = "stop-row active" if is_active else "stop-row"
        radio = (
            f'<input type="radio" name="{radio_name}" '
            f'value="{html.escape(s.stop_id)}" form="save-form"'
            + (" checked" if is_active else "")
            + ">"
        )
        rows.append(f"""
<label class="{cls}">
  {radio}
  <span class="name">{html.escape(s.display_name)}</span>
  <span class="meta">{s.distance_mi:.2f}&nbsp;mi</span>
</label>""")
    return "\n".join(rows)


def _subway_card_html(
    provider: transit.TransitProvider, state: dict[str, str],
) -> str:
    coords = _coords(state)
    if coords is None:
        return f"""
<section class="info-card provider-card">
  <div class="provider-card__head">
    <h2 class="provider-card__title">{html.escape(provider.label)}</h2>
    <span class="badge" style="--tone:var(--status-idle)">awaiting address</span>
  </div>
  <p class="provider-card__blurb">Enter your address above to find nearby subway stations.</p>
</section>"""

    stops = provider.find_stops_near(*coords, count=5)
    if not stops or stops[0].distance_mi > MAX_NEAREST_STOP_MILES:
        return f"""
<section class="info-card provider-card">
  <div class="provider-card__head">
    <h2 class="provider-card__title">{html.escape(provider.label)}</h2>
    <span class="badge" style="--tone:var(--status-idle)">no stations nearby</span>
  </div>
  <p class="provider-card__blurb">Nearest station is more than {MAX_NEAREST_STOP_MILES:.0f}&nbsp;mi away.</p>
</section>"""

    active_stop = _value_for(state, "JASPER_SUBWAY_STATION_ID")
    # Default-direction is unset in env when the user picked "both"
    # (or hasn't configured yet). Render "both" selected for that
    # case so a submit-without-touch round-trips to the same empty
    # value — selecting "uptown" by default would silently mutate
    # unconfigured state into "uptown" on the next save.
    active_dir = _value_for(state, "JASPER_SUBWAY_DEFAULT_DIRECTION").lower()
    if active_dir not in ("uptown", "downtown"):
        active_dir = "both"

    badge = _badge_html(bool(active_stop))
    rows_html = _stop_picker_rows_html(
        radio_name="nyc_subway_stop",
        stops=stops,
        active_id=active_stop,
    )
    dir_options = [
        ("uptown", "Uptown (Manhattan-bound at most stations)"),
        ("downtown", "Downtown (Coney/Brooklyn-bound at most stations)"),
        ("both", "Both directions"),
    ]
    dir_html = "".join(
        f'<option value="{html.escape(v)}"'
        + (' selected' if v == active_dir else '')
        + f'>{html.escape(label)}</option>'
        for v, label in dir_options
    )
    return f"""
<section class="info-card provider-card">
  <div class="provider-card__head">
    <h2 class="provider-card__title">{html.escape(provider.label)}</h2>
    {badge}
  </div>
  <p class="provider-card__blurb">Pick the station closest to home. &ldquo;Next train&rdquo; questions return every line that stops here, including trains rerouted from other lines during service changes.</p>
  {rows_html}

  <div class="field">
    <label for="nyc_subway_direction">Default direction</label>
    <select id="nyc_subway_direction" name="nyc_subway_direction" form="save-form">
      {dir_html}
    </select>
    <p class="form-hint">Used when the voice query doesn't name a direction. Pick &ldquo;Both&rdquo; if you want every train in either direction by default; the voice tool still honors a specific direction on request.</p>
  </div>
</section>"""


def _bus_card_html(
    provider: transit.TransitProvider, state: dict[str, str],
) -> str:
    coords = _coords(state)
    if coords is None:
        return f"""
<section class="info-card provider-card">
  <div class="provider-card__head">
    <h2 class="provider-card__title">{html.escape(provider.label)}</h2>
    <span class="badge" style="--tone:var(--status-idle)">awaiting address</span>
  </div>
  <p class="provider-card__blurb">Enter your address above to find nearby bus stops.</p>
</section>"""

    key_source = _bus_key_source(state)

    if key_source == "none":
        # Locked state: ONLY a register link + key input. Everything
        # else is intentionally hidden — there's nothing useful for
        # the user to do until they have a key.
        register_url = provider.credentials[0].help_url
        return f"""
<section class="info-card provider-card">
  <div class="provider-card__head">
    <h2 class="provider-card__title">{html.escape(provider.label)}</h2>
    <span class="badge" style="--tone:var(--status-warn)">needs an API key</span>
  </div>
  <div class="locked-card">
    <p>🔑 <strong>MTA BusTime needs a free API key</strong></p>
    <p>The endpoint that finds nearby bus stops requires a key. It's free, no payment info — but takes about 30 minutes to approve after you request one.</p>
    <p>
      <a class="btn btn--primary" href="{html.escape(register_url)}" target="_blank" rel="noopener">Get a free API key ↗</a>
    </p>
  </div>
  <div class="field">
    <label for="nyc_bus_key">{html.escape(provider.credentials[0].label)}</label>
    <input id="nyc_bus_key" name="nyc_bus_key" type="password"
           form="save-form"
           autocomplete="off" autocapitalize="off"
           autocorrect="off" spellcheck="false"
           placeholder="{html.escape(provider.credentials[0].placeholder)}">
    <p class="form-hint">Paste your key here, then save. We'll validate it against MTA, then show nearby stops on the next page.</p>
  </div>
</section>"""

    # Key is set — either in state (wizard-owned) or in env (operator
    # set via /etc/jasper/jasper.env, or post-migration via systemd
    # re-sourcing). Both unlock the card so the user can see what's
    # configured; the `env` source adds a yellow banner explaining
    # where the value lives.
    credentials = {"JASPER_MTA_BUSTIME_KEY": _value_for(state, "JASPER_MTA_BUSTIME_KEY")}
    error: str | None = None
    stops: list[transit.Stop] = []
    try:
        stops = provider.find_stops_near(*coords, credentials=credentials, count=8)
    except transit.TransitError as e:
        error = str(e)
    except Exception as e:  # noqa: BLE001
        logger.warning("bus stops fetch raised: %r", e)
        error = f"unexpected error: {e}"

    # SIRI-probe each candidate stop in parallel to enumerate the
    # routes ACTUALLY dispatching there. OBA's static `routes` field
    # lags real-world dispatch (the B70-at-4 Av/39 St case); SIRI is
    # ground truth. Fan out across stops to keep the render under the
    # nginx read timeout.
    siri_routes_by_stop: dict[str, tuple[str, ...]] = {}
    if stops and hasattr(provider, "enumerate_live_routes"):
        def _probe(stop_id: str) -> tuple[str, tuple[str, ...]]:
            try:
                return stop_id, provider.enumerate_live_routes(
                    stop_id, credentials=credentials,
                )
            except Exception as e:  # noqa: BLE001
                logger.info("SIRI probe failed for %s: %s", stop_id, e)
                return stop_id, ()
        with ThreadPoolExecutor(max_workers=len(stops)) as pool:
            for sid, routes in pool.map(_probe, [s.stop_id for s in stops]):
                siri_routes_by_stop[sid] = routes

    # Parse saved picks. JASPER_BUS_STOPS = "id|label,id|label"; the
    # wizard's hidden field carries the same format on POST. Build a
    # set of bare ids (no MTA_ prefix) for radio-state comparison.
    saved_picks = parse_bus_stops(_value_for(state, "JASPER_BUS_STOPS"))
    saved_ids_norm = {sid.removeprefix("MTA_") for sid, _ in saved_picks}

    badge = _badge_html(bool(saved_picks))

    error_html = ""
    if error:
        error_html = (
            f'<div class="banner banner--danger" role="status">Couldn\'t fetch '
            f'bus stops: {html.escape(error)}. Your saved configuration is '
            f'unchanged; try again later or use the Advanced section to enter a '
            f'stop ID manually.</div>'
        )
    elif not stops:
        error_html = (
            '<div class="banner banner--info" role="status">No bus stops within '
            '~1&nbsp;km of your coordinates.</div>'
        )

    # Soft-unlock banner: only render when the key is from os.environ
    # (operator-set externally) rather than the wizard's own file.
    # Saving in the wizard from this state writes the key into
    # transit.env for the first time; from then on the source flips to
    # 'state' and this banner disappears.
    external_notice_html = ""
    if key_source == "env":
        external_notice_html = (
            '<div class="banner banner--info" role="status">'
            'Detected an MTA BusTime API key in '
            '<code>/etc/jasper/jasper.env</code> (set outside the wizard). '
            'The daemon is using it already. Saving any change here will '
            'persist your picks (and the key) into '
            '<code>/var/lib/jasper/transit.env</code>, where the wizard '
            'owns it from then on.</div>'
        )

    # Masked-key readout — same shape as voice_setup.py shows for OAuth
    # secrets. Sourced from `_value_for` so it works whether the key
    # lives in state or in env. Empty string → render nothing.
    saved_key = _value_for(state, "JASPER_MTA_BUSTIME_KEY")
    masked = _mask_key(saved_key)
    key_source_label = {
        "state": "/var/lib/jasper/transit.env",
        "env": "/etc/jasper/jasper.env (external)",
    }.get(key_source, "")
    masked_key_html = (
        f'<p class="saved-key">Saved key: '
        f'<code>{html.escape(masked)}</code> '
        f'({html.escape(key_source_label)})</p>'
        if masked else ""
    )

    # Cluster stops by their MTA `name` field — both eastbound and
    # westbound at one intersection share that string. Inside each
    # cluster, list each direction separately so the user can pick
    # one, the other, or both. Preserve outer ordering (closest-first)
    # by using a dict (insertion-ordered) keyed on name.
    clusters: dict[str, list[transit.Stop]] = {}
    for s in stops:
        key = s.name or s.display_name
        clusters.setdefault(key, []).append(s)

    rows_html = ""
    if clusters:
        cluster_html: list[str] = []
        for name, group in clusters.items():
            # Routes shown for the cluster header are the union of
            # SIRI-enumerated routes across its stops, or the OBA
            # `lines` fallback when SIRI was silent.
            cluster_routes_set: set[str] = set()
            for s in group:
                live = siri_routes_by_stop.get(s.stop_id, ())
                cluster_routes_set.update(live or s.lines)
            cluster_routes = sorted(cluster_routes_set)

            heading_routes = (
                f'<span class="meta">{html.escape("/".join(cluster_routes))}</span>'
                if cluster_routes else ""
            )
            heading = f"""
<div class="cluster-heading">
  <strong>{html.escape(name)}</strong>
  {heading_routes}
</div>"""

            direction_rows: list[str] = []
            for s in group:
                bare = s.stop_id.removeprefix("MTA_")
                is_active = bare in saved_ids_norm
                cls = "stop-row active" if is_active else "stop-row"
                # Per-stop routes (SIRI ∪ OBA). When SIRI returned
                # nothing for this stop, fall through to whatever
                # OBA said (better than blank for off-peak stops).
                live = siri_routes_by_stop.get(s.stop_id, ())
                routes_here = live or s.lines
                routes_label = (
                    f' <span class="meta">{html.escape("/".join(routes_here))}</span>'
                    if routes_here else ""
                )
                dir_label = s.direction_hint or "—"
                # Each checkbox carries the stop_id + label as a
                # data-* attribute so the JS sync handler below can
                # rebuild the hidden `nyc_bus_stops` field's joined
                # value on every change.
                label_for_pipe = f"{name} {dir_label}".strip()
                checkbox = (
                    f'<input type="checkbox" class="bus-stop-pick" '
                    f'data-stop-id="{html.escape(s.stop_id)}" '
                    f'data-stop-label="{html.escape(label_for_pipe)}"'
                    + (" checked" if is_active else "")
                    + ">"
                )
                direction_rows.append(f"""
<label class="{cls}" style="padding-left:1.6em">
  {checkbox}
  <span class="name">{html.escape(dir_label)}</span>
  {routes_label}
  <span class="meta">{s.distance_mi:.2f}&nbsp;mi</span>
</label>""")
            cluster_html.append(
                heading + "\n" + "\n".join(direction_rows),
            )
        rows_html = "\n".join(cluster_html)

    # Serialise saved picks into the same id|label,id|label format the
    # form ships back. This is the initial value of the hidden input;
    # the JS sync handler updates it on every checkbox change.
    initial_picks_value = ",".join(
        f"{sid}|{label}" if label else sid
        for sid, label in saved_picks
    )

    # The hidden field round-trips the multi-select; the page's ES module
    # (deploy/assets/transit/js/main.js, syncPicker for .bus-stop-pick) keeps
    # it in lockstep with the checkboxes. Format matches `parse_bus_stops`:
    # "id|label,id|label".
    return f"""
<section class="info-card provider-card">
  <div class="provider-card__head">
    <h2 class="provider-card__title">{html.escape(provider.label)}</h2>
    {badge}
  </div>
  <p class="provider-card__blurb">Pick every bus stop near home you want included in &ldquo;next bus&rdquo; answers. Both directions at an intersection? Check both — the voice answer names each stop so you'll hear which is which.</p>
  {external_notice_html}
  {error_html}
  {rows_html}

  <input type="hidden" name="nyc_bus_stops" id="nyc-bus-stops-hidden"
         form="save-form"
         value="{html.escape(initial_picks_value)}">

  <details class="replace-key">
    <summary>Replace API key</summary>
    {masked_key_html}
    <div class="field">
      <label for="nyc_bus_key">{html.escape(provider.credentials[0].label)}</label>
      <input id="nyc_bus_key" name="nyc_bus_key" type="password"
             form="save-form"
             autocomplete="off" autocapitalize="off"
             autocorrect="off" spellcheck="false"
             placeholder="paste a new key to replace, or leave blank to keep">
    </div>
  </details>
</section>"""


def _citibike_card_html(
    provider: transit.TransitProvider, state: dict[str, str],
) -> str:
    """Citi Bike picker card.

    Keyless (GBFS is public), so unlike the bus card there's no
    locked state — once we have coords, render the household-wide
    e-bike-only toggle on top and the nearest stations underneath.
    Each station row shows a live snapshot (classic / ebikes / docks)
    so the user can pick informed; the voice tool re-fetches at
    every query so the snapshot is informational only."""
    coords = _coords(state)
    if coords is None:
        return f"""
<section class="info-card provider-card">
  <div class="provider-card__head">
    <h2 class="provider-card__title">{html.escape(provider.label)}</h2>
    <span class="badge" style="--tone:var(--status-idle)">awaiting address</span>
  </div>
  <p class="provider-card__blurb">Enter your address above to find nearby Citi Bike stations.</p>
</section>"""

    error: str | None = None
    stops: list[transit.Stop] = []
    try:
        stops = provider.find_stops_near(*coords, count=10)
    except transit.TransitError as e:
        error = str(e)
    except Exception as e:  # noqa: BLE001
        logger.warning("citibike stops fetch raised: %r", e)
        error = f"unexpected error: {e}"

    # Lazy-import via the runtime module — same cycle-break rationale
    # as in jasper.transit.providers.citibike (`jasper.citibike`'s
    # `from .transit.base import TransitError` triggers the registry
    # which loads the provider which would re-enter the runtime).
    from ..citibike import parse_saved_stations

    saved_picks = parse_saved_stations(_value_for(state, "JASPER_CITIBIKE_STATIONS"))
    saved_ids = {sid for sid, _ in saved_picks}
    ebike_only = (
        _value_for(state, "JASPER_CITIBIKE_EBIKE_ONLY", "").strip().lower()
        in {"1", "true", "yes"}
    )

    badge = _badge_html(bool(saved_picks))

    error_html = ""
    if error:
        error_html = (
            f'<div class="banner banner--danger" role="status">Couldn\'t fetch '
            f'Citi Bike stations: {html.escape(error)}. Your saved configuration '
            f'is unchanged; try again in a minute.</div>'
        )
    elif not stops:
        error_html = (
            '<div class="banner banner--info" role="status">No Citi Bike '
            'stations within range of your coordinates.</div>'
        )

    # Household-wide toggle. Sits above the picker because it changes the
    # meaning of the picker's rendered counts (you might want to ignore
    # stations with no e-bikes when this is on, even if they have plenty of
    # classic bikes). This is a native form control submitted with save-form,
    # so it can't use toggle_html() (which omits name/form); it reuses the
    # canonical `.toggle` CSS contract directly with the attributes the POST
    # needs. The label sits beside it in a `.toggle-row`.
    checked_attr = " checked" if ebike_only else ""
    ebike_checkbox_html = f"""
<div class="toggle-row">
  <span class="toggle-row__text">
    <span class="name">Only mention e-bikes in voice answers</span>
    <span class="meta">classic-bike counts are hidden when on</span>
  </span>
  <label class="toggle">
    <input type="checkbox" name="citibike_ebike_only" form="save-form"{checked_attr}>
    <span class="track"></span>
  </label>
</div>"""

    rows_html_parts: list[str] = []
    for s in stops:
        is_active = s.stop_id in saved_ids
        cls = "stop-row active" if is_active else "stop-row"
        # The provider packs the live snapshot ("4 classic, 3 e-bikes,
        # 25 docks") into `lines` as a single string. Render verbatim
        # in the meta column.
        snapshot = " / ".join(s.lines) if s.lines else ""
        checkbox = (
            f'<input type="checkbox" class="citibike-pick" '
            f'data-station-id="{html.escape(s.stop_id)}" '
            f'data-station-label="{html.escape(s.display_name)}"'
            + (" checked" if is_active else "")
            + ">"
        )
        meta_parts = [f"{s.distance_mi:.2f}&nbsp;mi"]
        if snapshot:
            meta_parts.append(html.escape(snapshot))
        meta_html = (
            '<span class="meta">' + " · ".join(meta_parts) + "</span>"
        )
        rows_html_parts.append(f"""
<label class="{cls}">
  {checkbox}
  <span class="name">{html.escape(s.display_name)}</span>
  {meta_html}
</label>""")
    rows_html = "\n".join(rows_html_parts)

    initial_picks_value = ",".join(
        f"{sid}|{label}" for sid, label in saved_picks
    )

    # Hidden field doubles as the "card was rendered" marker for
    # _apply_save — if it's missing from the POST, the card wasn't
    # shown (out-of-coverage user) and citibike state must not be
    # mutated. Always emit it, even when the picker is empty. The page's
    # ES module (syncPicker for .citibike-pick) keeps it in lockstep with
    # the checkboxes; format matches `parse_saved_stations`: "id|label,...".
    return f"""
<section class="info-card provider-card">
  <div class="provider-card__head">
    <h2 class="provider-card__title">{html.escape(provider.label)}</h2>
    {badge}
  </div>
  <p class="provider-card__blurb">Pick every Citi Bike station near home you want in answers. The voice answer splits e-bikes from classic bikes and reports open docks. Snapshot counts below are live at page load; the voice tool re-fetches every time you ask, so they go stale within ~30 seconds.</p>
  {error_html}

  <p class="eyebrow">Household-wide preference</p>
  {ebike_checkbox_html}

  <p class="eyebrow" style="margin-top:1.1rem">Stations near you</p>
  {rows_html}

  <input type="hidden" name="citibike_stations" id="citibike-stations-hidden"
         form="save-form"
         value="{html.escape(initial_picks_value)}">
</section>"""


def _travel_routes_card_html(
    state: dict[str, str],
    routes_state: dict[str, str],
) -> str:
    coords = _coords(state)
    configured = coords is not None and bool(_routes_key_value(routes_state))
    badge = _badge_html(configured)
    current_mode = _value_for(
        state,
        TRAVEL_DEFAULT_MODE_ENV,
        google_routes.DEFAULT_TRAVEL_MODE,
    )
    mode = google_routes.normalize_travel_mode(current_mode)
    if mode not in google_routes.TRAVEL_MODE_TO_API:
        mode = google_routes.DEFAULT_TRAVEL_MODE
    options = [
        ("transit", "Transit"),
        ("drive", "Drive"),
        ("walk", "Walk"),
        ("bicycle", "Bicycle"),
    ]
    options_html = "".join(
        f'<option value="{html.escape(value)}"'
        + (" selected" if value == mode else "")
        + f'>{html.escape(label)}</option>'
        for value, label in options
    )
    key_source = _routes_key_source(routes_state)
    saved_key = _routes_key_value(routes_state)
    masked = mask_secret(saved_key)
    source_label = {
        "state": "/var/lib/jasper-secrets/google_routes.env",
    }.get(key_source, "")
    saved_key_html = (
        f'<p class="saved-key">Saved key: '
        f'<code>{html.escape(masked)}</code> '
        f'({html.escape(source_label)})</p>'
        if masked else ""
    )
    key_input_html = """
<div class="field">
  <label for="google_routes_key">Google Routes API key</label>
  <input id="google_routes_key" name="google_routes_key" type="password"
         form="save-form"
         autocomplete="off" autocapitalize="off"
         autocorrect="off" spellcheck="false"
         placeholder="AIzaSy…">
  <p class="form-hint">Restrict this key to the Google Routes API. Saving does not call Google; the voice tool validates it on use.</p>
</div>"""
    if saved_key:
        key_input_html = f"""
<details class="replace-key">
  <summary>Replace API key</summary>
  {saved_key_html}
  <div class="field">
    <label for="google_routes_key">Google Routes API key</label>
    <input id="google_routes_key" name="google_routes_key" type="password"
           form="save-form"
           autocomplete="off" autocapitalize="off"
           autocorrect="off" spellcheck="false"
           placeholder="paste a new key to replace, or leave blank to keep">
    <p class="form-hint">The full key is never shown again after save.</p>
  </div>
  <label class="stop-row">
    <input type="checkbox" name="google_routes_clear_key" form="save-form" value="1">
    <span class="name">Clear saved Google Routes key</span>
  </label>
</details>"""
    return f"""
<section class="info-card provider-card">
  <div class="provider-card__head">
    <h2 class="provider-card__title">Travel time</h2>
    {badge}
  </div>
  <p class="provider-card__blurb">Use the saved location above as the starting point for &ldquo;how long to get to…&rdquo; and &ldquo;how can I get to…&rdquo; voice questions.</p>

  <div class="field">
    <label for="travel_default_mode">Default travel mode</label>
    <select id="travel_default_mode" name="travel_default_mode" form="save-form">
      {options_html}
    </select>
    <p class="form-hint">Voice instructions still override this, for example &ldquo;drive to&rdquo;, &ldquo;walk to&rdquo;, or &ldquo;take transit to&rdquo;.</p>
  </div>

  {key_input_html}
</section>"""


def _no_coverage_html() -> str:
    return """
<section class="no-coverage">
  <p><strong>No transit support for your area yet.</strong></p>
  <p>
    JTS bundles NYC subway and bus today. If you'd like another city or system
    (Berlin BVG, London TfL, Citi Bike, …), open an issue on
    <a href="https://github.com/jaspercurry/JTS/issues" target="_blank" rel="noopener">GitHub</a>.
    Adding one is a single new module under <code>jasper/transit/providers/</code> —
    see <code>nyc_subway.py</code> for the shape.
  </p>
  <p>Voice still works for everything else — you can skip transit setup entirely.</p>
</section>"""


def _advanced_section_html(state: dict[str, str], csrf_token: str) -> str:
    """Manual stop IDs / lat-lon. Sits behind a `<details>` so the
    median user never sees it; power users + recovery from a bad
    save path can use it without re-doing the address step."""
    lat, lon = "", ""
    coords = _coords(state)
    if coords is not None:
        lat = f"{coords[0]:.3f}"
        lon = f"{coords[1]:.3f}"
    sub_stop = _value_for(state, "JASPER_SUBWAY_STATION_ID")
    bus_stops_raw = _value_for(state, "JASPER_BUS_STOPS")
    return f"""
<details class="advanced">
  <summary>Advanced — enter coordinates or stop IDs manually</summary>
  <div class="advanced-body">
    <p class="form-hint">If you'd rather not geocode an address, paste coordinates from any map app. Three-decimal precision (~110&nbsp;m) is plenty.</p>
    <form method="post" action="geocode">
      {csrf_field_html(csrf_token)}
      <div class="field">
        <label for="manual_lat">Latitude</label>
        <input id="manual_lat" name="manual_lat" type="text"
               placeholder="40.646" value="{html.escape(lat)}">
      </div>
      <div class="field">
        <label for="manual_lon">Longitude</label>
        <input id="manual_lon" name="manual_lon" type="text"
               placeholder="-73.994" value="{html.escape(lon)}">
      </div>
      <div class="form-actions">
        <button type="submit" class="btn btn--default">Save coordinates</button>
      </div>
    </form>

    <p class="form-hint">Or override the picked stops directly. Useful if your stop didn't show up in the nearest list.</p>
    <div class="field">
      <label for="adv_sub_stop">Subway station ID</label>
      <input id="adv_sub_stop" name="nyc_subway_stop" type="text"
             form="save-form"
             placeholder="B12"
             value="{html.escape(sub_stop)}">
      <p class="form-hint">GTFS Stop ID (e.g. <code>B12</code> for 9 Av on the D). Look up at
        <a href="https://data.ny.gov/Transportation/MTA-Subway-Stations/39hk-dx4f" target="_blank" rel="noopener">data.ny.gov</a>.
      </p>
    </div>

    <div class="field">
      <label for="adv_bus_stops">Bus stops</label>
      <input id="adv_bus_stops" name="nyc_bus_stops" type="text"
             form="save-form"
             placeholder="MTA_302680|4 Av/39 St eastbound,MTA_302682|4 Av/39 St westbound"
             value="{html.escape(bus_stops_raw)}">
      <p class="form-hint">Comma-separated list. Each entry is <code>id</code> or <code>id|label</code>. Accepts either <code>MTA_302680</code> or just <code>302680</code>. Find IDs on the BusTime bus-stop sign or at <a href="https://bustime.mta.info/" target="_blank" rel="noopener">bustime.mta.info</a>.</p>
    </div>
  </div>
</details>"""


def _cities_section_html(
    state: dict[str, str], csrf_token: str, coords: tuple[float, float],
) -> str:
    """City-pack on/off toggles — the master switch for each city's transit.

    Shows every pack that either COVERS the user's coordinates or is
    currently ENABLED (so a pack enabled elsewhere can still be turned off).
    Returns "" when there is nothing to show. A pack being on only makes its
    providers *eligible*; the provider cards below render only for enabled
    packs, so this is where a household turns a whole city on or off.

    A covering-but-disabled pack surfaces an "available here" hint — the
    geocode-driven nudge to turn the detected city on. With a single pack
    (NYC today) this is one toggle; it scales to one row per future city.
    """
    lat, lon = coords
    enabled_ids = set(transit.enabled_pack_ids(state))
    rows: list[str] = []
    for pack in transit.CITY_PACKS:
        covers = pack.covers(lat, lon)
        is_on = pack.id in enabled_ids
        if not covers and not is_on:
            continue  # irrelevant here and already off — nothing to toggle
        if covers and is_on:
            meta = "covers your location"
        elif covers and not is_on:
            meta = "available at your location — turn on to use"
        else:  # on but not covering — surfaced so it can be turned off
            meta = "not near your saved location"
        checked = " checked" if is_on else ""
        rows.append(f"""
<div class="toggle-row">
  <span class="toggle-row__text">
    <span class="name">{html.escape(pack.label)}</span>
    <span class="meta">{meta}</span>
  </span>
  <label class="toggle">
    <input type="checkbox" name="city_{pack.id}" form="cities-form"{checked}>
    <span class="track"></span>
  </label>
</div>""")
    if not rows:
        return ""
    return f"""
<form method="post" action="cities" id="cities-form">
  {csrf_field_html(csrf_token) if csrf_token else ''}
  <section class="info-card">
    <p class="eyebrow">Transit cities</p>
    <p class="form-hint">Turn a city's transit on or off. Only enabled cities answer voice questions and show their settings below.</p>
    {''.join(rows)}
    <div class="save-row">
      <button type="submit" class="btn btn--primary">Save cities and restart voice</button>
    </div>
  </section>
</form>"""


def _index_html(
    state: dict[str, str],
    csrf_token: str = "",
    *,
    routes_state: dict[str, str] | None = None,
    status_msg: str = "",
    back_href: str = "/",
) -> bytes:
    coords = _coords(state)
    routes_state = routes_state or {}

    if coords is None:
        # No coords yet — only the address section is interactive.
        body = f"""
<p class="form-hint">Configure travel and transit settings for the speaker.</p>
{_address_section_html(state, csrf_token)}
{_advanced_section_html(state, csrf_token)}"""
        return _wrap_transit_page(
            "Transit", body, status_msg=status_msg, back_href=back_href,
        )

    providers_covering = transit.covering(*coords)
    if not providers_covering:
        # No provider covers these coords. Still render the cities section so
        # a pack enabled elsewhere (e.g. NYC left on after a move) can be
        # turned off; it returns "" when there's nothing to toggle.
        save_form = f"""
<form method="post" action="save" id="save-form">
  {csrf_field_html(csrf_token) if csrf_token else ''}
  <p class="eyebrow">Travel options</p>
  {_travel_routes_card_html(state, routes_state)}

  <div class="save-row">
    <button type="submit" class="btn btn--primary">Save and restart voice</button>
    <span class="form-hint">Voice picks up the new settings in about 5 seconds.</span>
  </div>
</form>"""
        body = f"""
<p class="form-hint">Configure travel and transit settings.</p>
{_address_section_html(state, csrf_token)}
{_cities_section_html(state, csrf_token, coords)}
{save_form}
{_no_coverage_html()}
{_advanced_section_html(state, csrf_token)}"""
        return _wrap_transit_page(
            "Transit", body, status_msg=status_msg, back_href=back_href,
        )

    # Per-provider card dispatch. Discovery (bbox + find_stops_near
    # + validate_credentials) is data-driven from the REGISTRY, but
    # each provider's wizard card is bespoke enough — subway has a
    # direction radio, bus has the locked-until-keyed state, future
    # Citi Bike would have a dock-capacity readout — that branching
    # here is honest. New providers add a branch; the unknown-id
    # fallback below keeps the page rendering while the contributor
    # wires up theirs. See jasper/transit/__init__.py for the full
    # contribution checklist.
    enabled_ids = set(transit.enabled_pack_ids(state))
    cards: list[str] = [_travel_routes_card_html(state, routes_state)]
    for p in providers_covering:
        pack = transit.pack_for_provider(p.id)
        if pack is not None and pack.id not in enabled_ids:
            # Covering, but its city is toggled off — gate the card out so
            # the page is honest (a visible card means its tools register).
            # The cities section above carries the toggle to turn it back on.
            continue
        if p.id == "nyc_subway":
            cards.append(_subway_card_html(p, state))
        elif p.id == "nyc_bus":
            cards.append(_bus_card_html(p, state))
        elif p.id == "citibike":
            cards.append(_citibike_card_html(p, state))
        else:
            cards.append(f"""
<section class="info-card provider-card">
  <div class="provider-card__head">
    <h2 class="provider-card__title">{html.escape(p.label)}</h2>
    <span class="badge" style="--tone:var(--status-idle)">no UI yet</span>
  </div>
  <p class="provider-card__blurb">This provider is in the registry but doesn't have a wizard card yet. Add one to <code>jasper/web/transit_setup.py</code>.</p>
</section>""")

    # The provider-pick save-form only renders when an enabled city has
    # covering providers. If every covering city is toggled off, `cards` is
    # empty and the cities section above already explains why there are no
    # settings to configure.
    save_form = ""
    if cards:
        save_form = f"""
<form method="post" action="save" id="save-form">
  {csrf_field_html(csrf_token) if csrf_token else ''}
  <p class="eyebrow">Travel and transit options</p>
  {''.join(cards)}

  <div class="save-row">
    <button type="submit" class="btn btn--primary">Save and restart voice</button>
    <span class="form-hint">Voice picks up the new settings in about 5 seconds.</span>
  </div>
</form>"""

    # The Clear form's destructive confirm lives in the page's ES module
    # (clear-form submit listener → jtsConfirm), not an inline onsubmit —
    # canonical pages carry no inline dialog helper.
    body = f"""
<p class="form-hint">Configure travel-time directions plus NYC subway and bus settings.</p>

{_address_section_html(state, csrf_token)}

{_cities_section_html(state, csrf_token, coords)}

{save_form}

{_advanced_section_html(state, csrf_token)}

<form method="post" action="clear" id="clear-form" style="margin-top:2rem">
  {csrf_field_html(csrf_token) if csrf_token else ''}
  <button type="submit" class="btn btn--danger">Clear all transit settings</button>
</form>"""
    return _wrap_transit_page(
        "Transit", body, status_msg=status_msg, back_href=back_href,
    )


# ----------------------------------------------------------------------
# HTTP handler.
# ----------------------------------------------------------------------


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    """Build the request handler closed over `cfg` (state-file path).
    Tests pass a tmpdir-based path; production uses TRANSIT_FILE."""
    cfg = {
        "state_path": cfg.get("state_path", TRANSIT_FILE),
        "routes_secret_path": cfg.get("routes_secret_path", GOOGLE_ROUTES_SECRET_FILE),
        "weather_path": cfg.get("weather_path", location_state.WEATHER_FILE),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(url.query)
            if path == "/":
                if not guard_read_request(self):
                    return
                state = _load_state(cfg["state_path"])
                ctx = begin_request(self)
                # Wrap render in a top-level guard: an unexpected
                # exception (corrupt CSV, malformed env file, etc.)
                # should yield a useful page with a diagnostic banner
                # rather than 500ing the whole route. The wizard's job
                # is to tell the user what to do next.
                try:
                    routes_state = read_env_file(cfg["routes_secret_path"])
                    body = _index_html(
                        state,
                        ctx["csrf_token"],
                        routes_state=routes_state,
                        status_msg=ctx["flash"],
                        back_href=safe_back_href(
                            (qs.get("return_to") or [""])[0],
                        ),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception("transit wizard render failed")
                    body = _wrap_transit_page(
                        "Transit",
                        f'<div class="banner banner--danger" role="status">'
                        f'Couldn\'t render the page: {html.escape(str(e))}. '
                        f'Check the daemon logs for the full traceback '
                        f'(<code>journalctl -u jasper-web</code>).</div>',
                    )
                send_html_response(self, body)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            # Route-check before CSRF-check: unknown paths return 404
            # without consuming the request body or revealing the CSRF
            # state. Matches what every test asserts.
            if path not in ("/geocode", "/save", "/clear", "/cities"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            form = read_form(self)
            if not guard_mutating_request(self, form):
                reject_csrf(self)
                return
            if path == "/geocode":
                self._handle_geocode(form)
                return
            if path == "/save":
                self._handle_save(form)
                return
            if path == "/cities":
                self._handle_cities(form)
                return
            if path == "/clear":
                self._handle_clear()
                return

        def _handle_geocode(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            new, err = _apply_geocode(form, current)
            if err is not None:
                send_see_other(self, "./", flash=err)
                return
            try:
                write_env_file(cfg["state_path"], new, mode=TRANSIT_FILE_MODE)
                _seed_weather_from_transit_if_missing(
                    new, weather_path=cfg["weather_path"],
                )
            except OSError as e:
                logger.exception("could not write transit.env after geocode")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
            display = new.get(DISPLAY_NAME_ENV, "")
            send_see_other(self, "./", flash=f"Found location: {display}")

        def _handle_save(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            new, err = _apply_save(form, current)
            if err is not None:
                send_see_other(self, "./", flash=err)
                return
            routes_current = read_env_file(cfg["routes_secret_path"])
            routes_new, routes_err = _apply_routes_save(form, routes_current)
            if routes_err is not None:
                send_see_other(self, "./", flash=routes_err)
                return
            try:
                if new:
                    write_env_file(cfg["state_path"], new, mode=TRANSIT_FILE_MODE)
                    _seed_weather_from_transit_if_missing(
                        new, weather_path=cfg["weather_path"],
                    )
                else:
                    delete_env_file(cfg["state_path"])
                if routes_new:
                    write_env_file(
                        cfg["routes_secret_path"],
                        routes_new,
                        mode=SECRET_ENV_MODE,
                    )
                else:
                    delete_env_file(cfg["routes_secret_path"])
            except OSError as e:
                logger.exception("could not write transit.env after save")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
            # No station/stop/dock IDs in the log — those reveal the
            # household's home location. Record only that a save landed.
            log_event(logger, "transit.save", client=self.address_string())
            restart_voice_daemon()
            send_see_other(self, "./", flash="Saved. Voice daemon restarting.")

        def _handle_cities(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            new = _apply_cities(form, current)
            # _apply_cities always sets JASPER_TRANSIT_CITIES (possibly empty),
            # so `new` is never an empty dict — write, never delete. (Coords are
            # normally present too, since the cities form only renders with
            # coords; a hand-crafted coords-less POST just persists the toggle,
            # which is harmless.)
            try:
                write_env_file(cfg["state_path"], new, mode=TRANSIT_FILE_MODE)
            except OSError as e:
                logger.exception("could not write transit.env after cities save")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
            log_event(
                logger,
                "transit.cities",
                cities=new.get(transit.TRANSIT_CITIES_ENV, ""),
                client=self.address_string(),
            )
            restart_voice_daemon()
            send_see_other(
                self, "./", flash="Saved cities. Voice daemon restarting.",
            )

        def _handle_clear(self) -> None:
            current = _load_state(cfg["state_path"])
            new = _apply_clear(current)
            # _apply_clear always records JASPER_TRANSIT_CITIES="" (present-empty
            # = "no cities"), so `new` is never empty — always write, never
            # delete. Deleting would drop the key back to ABSENT, which reads as
            # "all packs eligible" and would wrongly re-enable every city.
            try:
                write_env_file(cfg["state_path"], new, mode=TRANSIT_FILE_MODE)
                delete_env_file(cfg["routes_secret_path"])
            except OSError as e:
                logger.exception("could not write transit.env after clear")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
            log_event(logger, "transit.clear", client=self.address_string())
            restart_voice_daemon()
            send_see_other(
                self, "./",
                flash="Cleared transit settings. Voice restarting.",
            )

    return Handler


# ----------------------------------------------------------------------
# Entry points.
# ----------------------------------------------------------------------


def make_server(
    target,
    *,
    state_path: str = TRANSIT_FILE,
    routes_secret_path: str = GOOGLE_ROUTES_SECRET_FILE,
    weather_path: str = location_state.WEATHER_FILE,
) -> ThreadingHTTPServer:
    from . import _systemd
    cfg = {
        "state_path": state_path,
        "routes_secret_path": routes_secret_path,
        "weather_path": weather_path,
    }
    return _systemd.make_http_server(target, _make_handler(cfg))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-transit-web",
        description="Transit configuration UI for the Jasper smart speaker",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_TRANSIT_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_TRANSIT_WEB_PORT", "8777")),
    )
    parser.add_argument(
        "--state", default=os.environ.get("JASPER_TRANSIT_FILE", TRANSIT_FILE),
    )
    parser.add_argument(
        "--routes-secrets",
        default=os.environ.get("JASPER_GOOGLE_ROUTES_FILE", GOOGLE_ROUTES_SECRET_FILE),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server(
        (args.host, args.port),
        state_path=args.state,
        routes_secret_path=args.routes_secrets,
    )
    logger.info(
        "jasper-transit-web listening on http://%s:%d (state=%s)",
        args.host, args.port, args.state,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
