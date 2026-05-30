"""Transit configuration wizard at /transit/.

UX (single page, three sections):

  1. Address — user types a free-form address. Server geocodes via
     OSM Nominatim (Photon fallback) and stores coordinates in
     `/var/lib/jasper/transit.env`. Only coords land on disk; the
     address itself never persists.

  2. One card per provider whose bounding box covers the user's
     coords. The subway card is keyless and renders nearest stops
     immediately. The bus card is locked until the user pastes a
     BusTime API key — that's a hard prerequisite (the stops-lookup
     endpoint itself requires a key), so the locked card shows ONLY
     a register link + key input.

  3. Advanced — collapsed `<details>` with raw stop-ID / line / route
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
  POST /clear        wipe transit config; restart voice; redirects back
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from concurrent.futures import ThreadPoolExecutor

from .. import location_state, transit
from ..bus import parse_bus_stops
from ..transit import geocode as geocode_mod
from ._common import (
    PAGE_STYLE,
    begin_request,
    csrf_field_html,
    delete_env_file,
    read_env_file,
    reject_csrf,
    send_html_response,
    send_see_other,
    verify_csrf,
    read_form,
    restart_voice_daemon,
    wrap_page,
    write_env_file,
)

logger = logging.getLogger(__name__)


# Persisted at /var/lib/jasper/transit.env. Mode 0640 — the BusTime
# key is mildly sensitive but not as critical as an OAuth token.
TRANSIT_FILE = location_state.TRANSIT_FILE
TRANSIT_FILE_MODE = location_state.TRANSIT_FILE_MODE

# Wizard-owned coordinate state. Provider-owned env keys come from
# `transit.all_env_keys()`. Splitting these is deliberate: coords
# are wizard-internal scaffolding, not consumed by daemons directly.
LAT_ENV = location_state.TRANSIT_LAT_ENV
LON_ENV = location_state.TRANSIT_LON_ENV
DISPLAY_NAME_ENV = location_state.TRANSIT_DISPLAY_NAME_ENV

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
    return {LAT_ENV, LON_ENV, DISPLAY_NAME_ENV, *transit.all_env_keys()}


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


def _apply_clear(current: dict[str, str]) -> dict[str, str]:
    """Drop every wizard-owned key. Foreign keys (if any) survive."""
    return {
        k: v for k, v in current.items()
        if k not in _owned_env_keys()
    }


# ----------------------------------------------------------------------
# Page rendering.
# ----------------------------------------------------------------------


_TRANSIT_PAGE_STYLE = PAGE_STYLE + """
  .transit-help { color: #555; font-size: 0.93em;
                  margin: 0.4em 0 1.4em; line-height: 1.5; }
  .privacy-note { color: #666; font-size: 0.85em; margin: 0.4em 0 0;
                  line-height: 1.5; }
  .privacy-note a { color: #1db954; }

  /* Address geocode result panel. */
  .address-result {
    background: #f0fff4; border: 1px solid #1db954;
    padding: 0.7em 0.9em; border-radius: 6px;
    margin: 0.8em 0 1.2em;
    display: flex; align-items: flex-start; gap: 0.5em;
    flex-wrap: wrap;
  }
  .address-result .label { flex: 1; min-width: 200px; }
  .address-result .label strong { display: block; }
  .address-result .label .coords {
    color: #666; font-size: 0.85em; font-variant-numeric: tabular-nums;
  }
  .address-result form { margin: 0; }
  .address-result form button {
    background: #4a4a4a; padding: 0.4em 0.9em; font-size: 0.88em;
  }

  /* Provider card. Reuses .account/.account-body but each is always
     open since the page is configuration-flow, not browse-flow. */
  .provider-card {
    background: #f4f4f4; border-radius: 8px;
    margin-bottom: 1em; padding: 1em 1.2em;
    border: 1px solid #e6e6e6;
  }
  .provider-card h2 {
    margin: 0 0 0.2em; font-size: 1.08em;
    display: flex; align-items: center; gap: 0.6em;
  }
  .provider-card h2 .badge {
    background: #4a8; color: white;
    padding: 0.1em 0.55em; border-radius: 4px;
    font-size: 0.72em; font-weight: 500;
  }
  .provider-card h2 .badge.warn { background: #c80; }
  .provider-card h2 .badge.muted { background: #aaa; }
  .provider-card .blurb {
    color: #555; font-size: 0.9em; margin: 0 0 0.8em; line-height: 1.5;
  }

  /* Stop picker rows. */
  .stop-row {
    display: block; padding: 0.6em 0.8em; margin: 0.25em 0;
    background: white; border: 1px solid #e6e6e6; border-radius: 6px;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s;
  }
  .stop-row:hover { background: #f0fff4; border-color: #1db954; }
  .stop-row input[type=radio],
  .stop-row input[type=checkbox] {
    width: auto; flex: none; margin-right: 0.6em;
  }
  .stop-row.active { background: #f0fff4; border-color: #1db954; }
  .stop-row .name { font-weight: 600; }

  /* Bus-stop cluster header — groups opposing-direction stops at
     the same MTA-named intersection (e.g. "4 AV/39 ST"). The
     directions sit one indent below as `.stop-row` rows. */
  .cluster-heading {
    margin: 0.9em 0 0.2em; padding: 0.2em 0;
    display: flex; align-items: baseline; gap: 0.5em;
  }
  .cluster-heading strong { color: #222; font-size: 0.98em; }
  .cluster-heading .meta { color: #888; font-size: 0.85em; }

  .stop-row .meta {
    color: #888; font-size: 0.85em;
    font-variant-numeric: tabular-nums;
    margin-left: 0.4em;
  }

  /* Locked bus card — restrict to "go register + paste key". */
  .locked-card {
    background: #fff7e6; border: 1px solid #f0c060;
    padding: 0.9em 1em; border-radius: 6px;
    margin: 0.5em 0 1em;
  }
  .locked-card .icon {
    font-size: 1.1em; margin-right: 0.4em;
  }
  .locked-card p { margin: 0 0 0.6em; line-height: 1.5; }

  /* Advanced section. */
  details.advanced { margin-top: 1.5em; }
  details.advanced > summary {
    cursor: pointer; padding: 0.6em 0.8em; border-radius: 6px;
    background: #f4f4f4; border: 1px solid #e6e6e6;
    font-weight: 600; color: #444;
    user-select: none; -webkit-user-select: none;
  }
  details.advanced > summary:hover { background: #ececec; }
  details.advanced .advanced-body {
    padding: 0.8em 0.4em 0.4em; border: 1px solid #e6e6e6;
    border-top: none; border-radius: 0 0 6px 6px;
    margin-top: -1px;
  }
  details.advanced label { margin-top: 0.7em; }

  /* No-coverage card. */
  .no-coverage {
    background: #fafafa; border: 1px dashed #d0d0d0;
    padding: 1em; border-radius: 6px; color: #555;
    line-height: 1.55;
  }

  /* Save button row. */
  .save-row { margin-top: 1.6em; display: flex;
              gap: 0.6em; align-items: center; }
"""


def _wrap_transit_page(title: str, body: str, *, status_msg: str = "") -> bytes:
    page = wrap_page(title, body, status_msg=status_msg).decode()
    return page.replace(
        f"<style>{PAGE_STYLE}</style>",
        f"<style>{_TRANSIT_PAGE_STYLE}</style>",
    ).encode()


def _address_section_html(state: dict[str, str], csrf_token: str) -> str:
    coords = _coords(state)
    display = _value_for(state, DISPLAY_NAME_ENV)
    csrf = csrf_field_html(csrf_token)

    if coords is not None:
        lat, lon = coords
        # "Found you here" panel with a Re-geocode form.
        return f"""
<h2>Where you are</h2>
<div class="address-result">
  <div class="label">
    <strong>{html.escape(display) or "(saved location)"}</strong>
    <span class="coords">{lat:.3f}, {lon:.3f} (~110&nbsp;m precision)</span>
  </div>
  <form method="post" action="geocode">
    {csrf}
    <input type="hidden" name="_redo" value="1">
    <button type="button" onclick="document.getElementById('redo-form').style.display='block';this.parentElement.style.display='none';">Change…</button>
  </form>
</div>
<form method="post" action="geocode" id="redo-form" style="display:none">
  {csrf}
  <label for="address-redo">New address</label>
  <input id="address-redo" name="address" type="text"
         placeholder="123 Main St, Brooklyn NY"
         autocomplete="street-address">
  <p class="privacy-note">
    Your address is sent to <a href="https://nominatim.openstreetmap.org/" target="_blank" rel="noopener">OpenStreetMap (Nominatim)</a>
    to look up coordinates. Only the coordinates (rounded to ~110&nbsp;m) are saved on this speaker.
    <a href="https://operations.osmfoundation.org/policies/nominatim/" target="_blank" rel="noopener">Policy ↗</a>
  </p>
  <button type="submit">Find nearby stops</button>
</form>"""

    # Cold state — no coords yet. Big address input as the only thing
    # the user can do.
    return f"""
<h2>Where you are</h2>
<p class="transit-help">
  Enter your home address. We'll use it to find nearby transit stops.
</p>
<form method="post" action="geocode">
  {csrf}
  <label for="address">Home address</label>
  <input id="address" name="address" type="text"
         placeholder="123 Main St, Brooklyn NY"
         autocomplete="street-address" autofocus>
  <p class="privacy-note">
    Your address is sent to <a href="https://nominatim.openstreetmap.org/" target="_blank" rel="noopener">OpenStreetMap (Nominatim)</a>
    to look up coordinates. Only the coordinates (rounded to ~110&nbsp;m) are saved on this speaker — never the address itself.
    <a href="https://operations.osmfoundation.org/policies/nominatim/" target="_blank" rel="noopener">Policy ↗</a>
  </p>
  <button type="submit">Find nearby stops</button>
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
<section class="provider-card">
  <h2>{html.escape(provider.label)} <span class="badge muted">awaiting address</span></h2>
  <p class="blurb">Enter your address above to find nearby subway stations.</p>
</section>"""

    stops = provider.find_stops_near(*coords, count=5)
    if not stops or stops[0].distance_mi > MAX_NEAREST_STOP_MILES:
        return f"""
<section class="provider-card">
  <h2>{html.escape(provider.label)} <span class="badge muted">no stations nearby</span></h2>
  <p class="blurb">Nearest station is more than {MAX_NEAREST_STOP_MILES:.0f}&nbsp;mi away.</p>
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

    badge = (
        '<span class="badge">configured</span>' if active_stop
        else '<span class="badge muted">not configured</span>'
    )
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
<section class="provider-card">
  <h2>{html.escape(provider.label)} {badge}</h2>
  <p class="blurb">Pick the station closest to home. &ldquo;Next train&rdquo; questions return every line that stops here, including trains rerouted from other lines during service changes.</p>
  {rows_html}

  <label for="nyc_subway_direction">Default direction</label>
  <select id="nyc_subway_direction" name="nyc_subway_direction" form="save-form">
    {dir_html}
  </select>
  <small>Used when the voice query doesn't name a direction. Pick &ldquo;Both&rdquo; if you want every train in either direction by default; the voice tool still honors a specific direction on request.</small>
</section>"""


def _bus_card_html(
    provider: transit.TransitProvider, state: dict[str, str],
) -> str:
    coords = _coords(state)
    if coords is None:
        return f"""
<section class="provider-card">
  <h2>{html.escape(provider.label)} <span class="badge muted">awaiting address</span></h2>
  <p class="blurb">Enter your address above to find nearby bus stops.</p>
</section>"""

    key_source = _bus_key_source(state)

    if key_source == "none":
        # Locked state: ONLY a register link + key input. Everything
        # else is intentionally hidden — there's nothing useful for
        # the user to do until they have a key.
        register_url = provider.credentials[0].help_url
        return f"""
<section class="provider-card">
  <h2>{html.escape(provider.label)} <span class="badge warn">needs an API key</span></h2>
  <div class="locked-card">
    <p><span class="icon">🔑</span><strong>MTA BusTime needs a free API key</strong></p>
    <p>The endpoint that finds nearby bus stops requires a key. It's free, no payment info — but takes about 30 minutes to approve after you request one.</p>
    <p>
      <a class="btn" href="{html.escape(register_url)}" target="_blank" rel="noopener">Get a free API key ↗</a>
    </p>
  </div>
  <label for="nyc_bus_key">{html.escape(provider.credentials[0].label)}</label>
  <input id="nyc_bus_key" name="nyc_bus_key" type="password"
         form="save-form"
         autocomplete="off" autocapitalize="off"
         autocorrect="off" spellcheck="false"
         placeholder="{html.escape(provider.credentials[0].placeholder)}">
  <small>Paste your key here, then save. We'll validate it against MTA, then show nearby stops on the next page.</small>
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

    badge = (
        '<span class="badge">configured</span>' if saved_picks
        else '<span class="badge muted">not configured</span>'
    )

    error_html = ""
    if error:
        error_html = (
            f'<p class="msg err">Couldn\'t fetch bus stops: {html.escape(error)}. '
            f'Your saved configuration is unchanged; try again later or use the '
            f'<em>Advanced</em> section to enter a stop ID manually.</p>'
        )
    elif not stops:
        error_html = (
            '<p class="msg">No bus stops within ~1&nbsp;km of your coordinates.</p>'
        )

    # Soft-unlock banner: only render when the key is from os.environ
    # (operator-set externally) rather than the wizard's own file.
    # Saving in the wizard from this state writes the key into
    # transit.env for the first time; from then on the source flips to
    # 'state' and this banner disappears.
    external_notice_html = ""
    if key_source == "env":
        external_notice_html = (
            '<p class="msg" style="background:#fff7e6;border-color:#f0c060;color:#5a4500">'
            'ℹ️ Detected an MTA BusTime API key in '
            '<code>/etc/jasper/jasper.env</code> (set outside the wizard). '
            'The daemon is using it already. Saving any change here will '
            'persist your picks (and the key) into '
            '<code>/var/lib/jasper/transit.env</code>, where the wizard '
            'owns it from then on.</p>'
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
        f'<p class="meta" style="margin-top:0.4em">Saved key: '
        f'<code>{html.escape(masked)}</code> '
        f'<span style="color:#888">({html.escape(key_source_label)})</span></p>'
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

    return f"""
<section class="provider-card">
  <h2>{html.escape(provider.label)} {badge}</h2>
  <p class="blurb">Pick every bus stop near home you want included in &ldquo;next bus&rdquo; answers. Both directions at an intersection? Check both — the voice answer names each stop so you'll hear which is which.</p>
  {external_notice_html}
  {error_html}
  {rows_html}

  <input type="hidden" name="nyc_bus_stops" id="nyc-bus-stops-hidden"
         form="save-form"
         value="{html.escape(initial_picks_value)}">
  <script>
    // Sync checkbox state into the hidden field on every change so a
    // multi-select round-trips cleanly through the urlencoded form.
    // The hidden value format matches `parse_bus_stops` in
    // jasper/bus.py: "id|label,id|label".
    (function() {{
      var hidden = document.getElementById('nyc-bus-stops-hidden');
      function sync() {{
        var parts = [];
        document.querySelectorAll('.bus-stop-pick:checked').forEach(function(cb) {{
          var sid = cb.dataset.stopId || '';
          var label = (cb.dataset.stopLabel || '').replace(/[|,]/g, ' ');
          parts.push(label ? (sid + '|' + label) : sid);
        }});
        hidden.value = parts.join(',');
      }}
      document.querySelectorAll('.bus-stop-pick').forEach(function(cb) {{
        cb.addEventListener('change', sync);
      }});
      sync();  // initial reconciliation
    }})();
  </script>

  <details style="margin-top: 1em">
    <summary style="cursor:pointer; color:#666; font-size:0.9em">Replace API key</summary>
    {masked_key_html}
    <label for="nyc_bus_key">{html.escape(provider.credentials[0].label)}</label>
    <input id="nyc_bus_key" name="nyc_bus_key" type="password"
           form="save-form"
           autocomplete="off" autocapitalize="off"
           autocorrect="off" spellcheck="false"
           placeholder="paste a new key to replace, or leave blank to keep">
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
<section class="provider-card">
  <h2>{html.escape(provider.label)} <span class="badge muted">awaiting address</span></h2>
  <p class="blurb">Enter your address above to find nearby Citi Bike stations.</p>
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

    badge = (
        '<span class="badge">configured</span>' if saved_picks
        else '<span class="badge muted">not configured</span>'
    )

    error_html = ""
    if error:
        error_html = (
            f'<p class="msg err">Couldn\'t fetch Citi Bike stations: '
            f'{html.escape(error)}. Your saved configuration is unchanged; '
            f'try again in a minute.</p>'
        )
    elif not stops:
        error_html = (
            '<p class="msg">No Citi Bike stations within range of your '
            'coordinates.</p>'
        )

    # Household-wide toggle. Sits above the picker because it changes
    # the meaning of the picker's rendered counts (you might want to
    # ignore stations with no e-bikes when this is on, even if they
    # have plenty of classic bikes).
    ebike_checkbox_html = f"""
<label class="stop-row" style="background:#f4f8ff; border-color:#9bb7d4">
  <input type="checkbox" name="citibike_ebike_only" form="save-form"{' checked' if ebike_only else ''}>
  <span class="name">Only mention e-bikes in voice answers</span>
  <span class="meta">classic-bike counts are hidden when on</span>
</label>"""

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
    # mutated. Always emit it, even when the picker is empty.
    return f"""
<section class="provider-card">
  <h2>{html.escape(provider.label)} {badge}</h2>
  <p class="blurb">Pick every Citi Bike station near home you want in answers. The voice answer splits e-bikes from classic bikes and reports open docks. Snapshot counts below are live at page load; the voice tool re-fetches every time you ask, so they go stale within ~30 seconds.</p>
  {error_html}

  <h3 style="margin: 1.2em 0 0.4em; font-size: 0.95em; color: #444">Household-wide preference</h3>
  {ebike_checkbox_html}

  <h3 style="margin: 1.2em 0 0.4em; font-size: 0.95em; color: #444">Stations near you</h3>
  {rows_html}

  <input type="hidden" name="citibike_stations" id="citibike-stations-hidden"
         form="save-form"
         value="{html.escape(initial_picks_value)}">
  <script>
    // Mirror of the bus-stops sync — keep the hidden field in lockstep
    // with checkbox state so the multi-select round-trips through
    // urlencoded POST. Format matches `parse_saved_stations` in
    // jasper/citibike.py: "id|label,id|label".
    (function() {{
      var hidden = document.getElementById('citibike-stations-hidden');
      function sync() {{
        var parts = [];
        document.querySelectorAll('.citibike-pick:checked').forEach(function(cb) {{
          var sid = cb.dataset.stationId || '';
          var label = (cb.dataset.stationLabel || '').replace(/[|,]/g, ' ');
          parts.push(label ? (sid + '|' + label) : sid);
        }});
        hidden.value = parts.join(',');
      }}
      document.querySelectorAll('.citibike-pick').forEach(function(cb) {{
        cb.addEventListener('change', sync);
      }});
      sync();
    }})();
  </script>
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
    <p class="hint">If you'd rather not geocode an address, paste coordinates from any map app. Three-decimal precision (~110&nbsp;m) is plenty.</p>
    <form method="post" action="geocode" style="margin-bottom:1em">
      {csrf_field_html(csrf_token)}
      <label for="manual_lat">Latitude</label>
      <input id="manual_lat" name="manual_lat" type="text"
             placeholder="40.646" value="{html.escape(lat)}">
      <label for="manual_lon">Longitude</label>
      <input id="manual_lon" name="manual_lon" type="text"
             placeholder="-73.994" value="{html.escape(lon)}">
      <button type="submit" class="secondary">Save coordinates</button>
    </form>

    <p class="hint">Or override the picked stops directly. Useful if your stop didn't show up in the nearest list.</p>
    <label for="adv_sub_stop">Subway station ID</label>
    <input id="adv_sub_stop" name="nyc_subway_stop" type="text"
           form="save-form"
           placeholder="B12"
           value="{html.escape(sub_stop)}">
    <small>GTFS Stop ID (e.g. <code>B12</code> for 9 Av on the D). Look up at
      <a href="https://data.ny.gov/Transportation/MTA-Subway-Stations/39hk-dx4f" target="_blank" rel="noopener">data.ny.gov</a>.
    </small>

    <label for="adv_bus_stops">Bus stops</label>
    <input id="adv_bus_stops" name="nyc_bus_stops" type="text"
           form="save-form"
           placeholder="MTA_302680|4 Av/39 St eastbound,MTA_302682|4 Av/39 St westbound"
           value="{html.escape(bus_stops_raw)}">
    <small>Comma-separated list. Each entry is <code>id</code> or <code>id|label</code>. Accepts either <code>MTA_302680</code> or just <code>302680</code>. Find IDs on the BusTime bus-stop sign or at <a href="https://bustime.mta.info/" target="_blank" rel="noopener">bustime.mta.info</a>.</small>
  </div>
</details>"""


def _index_html(state: dict[str, str], csrf_token: str = "", *, status_msg: str = "") -> bytes:
    coords = _coords(state)

    if coords is None:
        # No coords yet — only the address section is interactive.
        body = f"""
<p class="sub">Configure NYC subway and bus settings so you can ask "next train" / "next bus" from the speaker.</p>
{_address_section_html(state, csrf_token)}
{_advanced_section_html(state, csrf_token)}"""
        return _wrap_transit_page("Transit", body, status_msg=status_msg)

    providers_covering = transit.covering(*coords)
    if not providers_covering:
        body = f"""
<p class="sub">Configure transit settings.</p>
{_address_section_html(state, csrf_token)}
{_no_coverage_html()}
{_advanced_section_html(state, csrf_token)}"""
        return _wrap_transit_page("Transit", body, status_msg=status_msg)

    # Per-provider card dispatch. Discovery (bbox + find_stops_near
    # + validate_credentials) is data-driven from the REGISTRY, but
    # each provider's wizard card is bespoke enough — subway has a
    # direction radio, bus has the locked-until-keyed state, future
    # Citi Bike would have a dock-capacity readout — that branching
    # here is honest. New providers add a branch; the unknown-id
    # fallback below keeps the page rendering while the contributor
    # wires up theirs. See jasper/transit/__init__.py for the full
    # contribution checklist.
    cards: list[str] = []
    for p in providers_covering:
        if p.id == "nyc_subway":
            cards.append(_subway_card_html(p, state))
        elif p.id == "nyc_bus":
            cards.append(_bus_card_html(p, state))
        elif p.id == "citibike":
            cards.append(_citibike_card_html(p, state))
        else:
            cards.append(f"""
<section class="provider-card">
  <h2>{html.escape(p.label)} <span class="badge muted">no UI yet</span></h2>
  <p class="blurb">This provider is in the registry but doesn't have a wizard card yet. Add one to <code>jasper/web/transit_setup.py</code>.</p>
</section>""")

    body = f"""
<p class="sub">Configure NYC subway and bus settings so you can ask "next train" / "next bus" from the speaker.</p>

{_address_section_html(state, csrf_token)}

<form method="post" action="save" id="save-form">
  {csrf_field_html(csrf_token) if csrf_token else ''}
  <h2>Transit options near you</h2>
  {''.join(cards)}

  <div class="save-row">
    <button type="submit">Save and restart voice</button>
    <span class="hint">Voice picks up the new settings in about 5 seconds.</span>
  </div>
</form>

{_advanced_section_html(state, csrf_token)}

<p class="hint" style="margin-top:2em">
  <form method="post" action="clear" style="display:inline" onsubmit="return jtsConfirmSubmit(this, 'Clear all saved transit settings? Subway and bus tools will stop responding until reconfigured.', {{danger:true}});">
    {csrf_field_html(csrf_token) if csrf_token else ''}
    <button type="submit" class="danger">Clear all transit settings</button>
  </form>
</p>"""
    return _wrap_transit_page("Transit", body, status_msg=status_msg)


# ----------------------------------------------------------------------
# HTTP handler.
# ----------------------------------------------------------------------


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    """Build the request handler closed over `cfg` (state-file path).
    Tests pass a tmpdir-based path; production uses TRANSIT_FILE."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                state = _load_state(cfg["state_path"])
                ctx = begin_request(self)
                # Wrap render in a top-level guard: an unexpected
                # exception (corrupt CSV, malformed env file, etc.)
                # should yield a useful page with a diagnostic banner
                # rather than 500ing the whole route. The wizard's job
                # is to tell the user what to do next.
                try:
                    body = _index_html(
                        state, ctx["csrf_token"], status_msg=ctx["flash"],
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception("transit wizard render failed")
                    body = _wrap_transit_page(
                        "Transit",
                        f'<p class="msg err">Couldn\'t render the page: '
                        f'{html.escape(str(e))}. Check the daemon logs for '
                        f'the full traceback (<code>journalctl -u jasper-web</code>).</p>',
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
            if path not in ("/geocode", "/save", "/clear"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            form = read_form(self)
            if not verify_csrf(self, form):
                reject_csrf(self)
                return
            if path == "/geocode":
                self._handle_geocode(form)
                return
            if path == "/save":
                self._handle_save(form)
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
            try:
                if new:
                    write_env_file(cfg["state_path"], new, mode=TRANSIT_FILE_MODE)
                    _seed_weather_from_transit_if_missing(
                        new, weather_path=cfg["weather_path"],
                    )
                else:
                    delete_env_file(cfg["state_path"])
            except OSError as e:
                logger.exception("could not write transit.env after save")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
            restart_voice_daemon()
            send_see_other(self, "./", flash="Saved. Voice daemon restarting.")

        def _handle_clear(self) -> None:
            current = _load_state(cfg["state_path"])
            new = _apply_clear(current)
            try:
                if new:
                    write_env_file(cfg["state_path"], new, mode=TRANSIT_FILE_MODE)
                else:
                    delete_env_file(cfg["state_path"])
            except OSError as e:
                logger.exception("could not write transit.env after clear")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
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
    weather_path: str = location_state.WEATHER_FILE,
) -> ThreadingHTTPServer:
    from . import _systemd
    cfg = {"state_path": state_path, "weather_path": weather_path}
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
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server((args.host, args.port), state_path=args.state)
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
