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

from .. import transit
from ..transit import geocode as geocode_mod
from ._common import (
    PAGE_STYLE,
    delete_env_file,
    read_env_file,
    read_form,
    restart_voice_daemon,
    wrap_page,
    write_env_file,
)

logger = logging.getLogger(__name__)


# Persisted at /var/lib/jasper/transit.env. Mode 0640 — the BusTime
# key is mildly sensitive but not as critical as an OAuth token.
TRANSIT_FILE = "/var/lib/jasper/transit.env"
TRANSIT_FILE_MODE = 0o640

# Wizard-owned coordinate state. Provider-owned env keys come from
# `transit.all_env_keys()`. Splitting these is deliberate: coords
# are wizard-internal scaffolding, not consumed by daemons directly.
LAT_ENV = "JASPER_TRANSIT_LAT"
LON_ENV = "JASPER_TRANSIT_LON"
DISPLAY_NAME_ENV = "JASPER_TRANSIT_DISPLAY_NAME"

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
    return bool(_value_for(state, "JASPER_MTA_BUSTIME_KEY"))


def _split_list(raw: str) -> list[str]:
    """Parse a comma/space-separated value the same way config.py does
    (mta_routes uses the same shape). Empty inputs return an empty
    list; whitespace tolerance is intentional."""
    return [t.strip() for t in raw.replace(",", " ").split() if t.strip()]


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
    if sub_dir in ("uptown", "downtown", "both", ""):
        if sub_dir and sub_dir != "both":
            new["JASPER_SUBWAY_DEFAULT_DIRECTION"] = sub_dir
        elif sub_dir == "both":
            # Empty value = no default direction; daemon prompts on bare
            # "next train" queries.
            new.pop("JASPER_SUBWAY_DEFAULT_DIRECTION", None)
    sub_lines_raw = form.get("nyc_subway_lines")
    if sub_lines_raw is not None:
        parsed = _split_list(sub_lines_raw)
        if parsed:
            new["JASPER_SUBWAY_LINES"] = ",".join(parsed)
        else:
            new.pop("JASPER_SUBWAY_LINES", None)

    # Bus key — pasted means replace; blank means keep. The lookup
    # endpoint requires a key, so we validate on paste.
    new_key = (form.get("nyc_bus_key") or "").strip()
    if new_key:
        if bus_provider is None:
            return current, "Bus provider unavailable."
        try:
            ok = bus_provider.validate_credential(
                "JASPER_MTA_BUSTIME_KEY", new_key,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("bus credential probe raised: %r", e)
            ok = False
        if not ok:
            return current, (
                "MTA BusTime rejected that key. Double-check you copied it "
                "correctly, or wait a few minutes — fresh keys can take ~30 "
                "minutes to activate."
            )
        new["JASPER_MTA_BUSTIME_KEY"] = new_key

    # Bus stop pick. Same "blank means keep" semantics as subway.
    bus_stop = (form.get("nyc_bus_stop") or "").strip()
    if bus_stop:
        new["JASPER_BUS_STOP_ID"] = bus_stop
    bus_routes_raw = form.get("nyc_bus_routes")
    if bus_routes_raw is not None:
        parsed = _split_list(bus_routes_raw)
        if parsed:
            new["JASPER_BUS_ROUTES"] = ",".join(parsed)
        else:
            new.pop("JASPER_BUS_ROUTES", None)

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
  .stop-row input[type=radio] {
    width: auto; flex: none; margin-right: 0.6em;
  }
  .stop-row.active { background: #f0fff4; border-color: #1db954; }
  .stop-row .name { font-weight: 600; }
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


def _address_section_html(state: dict[str, str]) -> str:
    coords = _coords(state)
    display = _value_for(state, DISPLAY_NAME_ENV)

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
    <input type="hidden" name="_redo" value="1">
    <button type="button" onclick="document.getElementById('redo-form').style.display='block';this.parentElement.style.display='none';">Change…</button>
  </form>
</div>
<form method="post" action="geocode" id="redo-form" style="display:none">
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
    return """
<h2>Where you are</h2>
<p class="transit-help">
  Enter your home address. We'll use it to find nearby transit stops.
</p>
<form method="post" action="geocode">
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
    active_dir = (
        _value_for(state, "JASPER_SUBWAY_DEFAULT_DIRECTION").lower()
        or "uptown"
    )
    active_lines = _value_for(state, "JASPER_SUBWAY_LINES")

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
        ("both", "Both — ask each time"),
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
  <p class="blurb">Pick the station closest to home. Voice answers will default to this one when you ask for the next train.</p>
  {rows_html}

  <label for="nyc_subway_direction">Default direction</label>
  <select id="nyc_subway_direction" name="nyc_subway_direction" form="save-form">
    {dir_html}
  </select>

  <label for="nyc_subway_lines">Lines that stop here (optional)</label>
  <input id="nyc_subway_lines" name="nyc_subway_lines" type="text"
         form="save-form"
         placeholder="D"
         value="{html.escape(active_lines)}">
  <small>Comma- or space-separated. Leave blank to allow any line that serves the station.</small>
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

    if not _has_bus_key(state):
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

    # Key set. Fetch nearby stops via BusTime. Failure here is
    # non-fatal — keep the card up with an actionable error.
    credentials = {"JASPER_MTA_BUSTIME_KEY": _value_for(state, "JASPER_MTA_BUSTIME_KEY")}
    error: str | None = None
    stops: list[transit.Stop] = []
    try:
        stops = provider.find_stops_near(*coords, credentials=credentials, count=5)
    except transit.TransitError as e:
        error = str(e)
    except Exception as e:  # noqa: BLE001
        logger.warning("bus stops fetch raised: %r", e)
        error = f"unexpected error: {e}"

    active_stop = _value_for(state, "JASPER_BUS_STOP_ID")
    # OBA returns "MTA_302680"; saved value may match either with or
    # without the prefix. Normalise for comparison so the active radio
    # checks correctly regardless of which form the user saved.
    active_stop_norm = active_stop.removeprefix("MTA_")
    active_routes = _value_for(state, "JASPER_BUS_ROUTES")

    badge = (
        '<span class="badge">configured</span>' if active_stop
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
            '<p class="msg">No bus stops within 1&nbsp;km of your coordinates.</p>'
        )

    # Match either OBA form (with or without MTA_ prefix) against the
    # saved active id so the right row stays checked across save flows.
    stops_with_norm: list[transit.Stop] = []
    for s in stops:
        stops_with_norm.append(s)
    rows_html = ""
    if stops:
        rows: list[str] = []
        for s in stops:
            norm = s.stop_id.removeprefix("MTA_")
            is_active = norm == active_stop_norm
            cls = "stop-row active" if is_active else "stop-row"
            radio = (
                f'<input type="radio" name="nyc_bus_stop" '
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
        rows_html = "\n".join(rows)

    return f"""
<section class="provider-card">
  <h2>{html.escape(provider.label)} {badge}</h2>
  <p class="blurb">Pick the bus stop closest to home. Each stop is direction-specific — the eastbound and westbound stops on the same street have different IDs.</p>
  {error_html}
  {rows_html}

  <label for="nyc_bus_routes">Routes to track (optional)</label>
  <input id="nyc_bus_routes" name="nyc_bus_routes" type="text"
         form="save-form"
         placeholder="B35, B70"
         value="{html.escape(active_routes)}">
  <small>Comma- or space-separated. Leave blank to show all routes at the stop.</small>

  <details style="margin-top: 1em">
    <summary style="cursor:pointer; color:#666; font-size:0.9em">Replace API key</summary>
    <label for="nyc_bus_key">{html.escape(provider.credentials[0].label)}</label>
    <input id="nyc_bus_key" name="nyc_bus_key" type="password"
           form="save-form"
           autocomplete="off" autocapitalize="off"
           autocorrect="off" spellcheck="false"
           placeholder="paste a new key to replace, or leave blank to keep">
  </details>
</section>"""


def _no_coverage_html() -> str:
    return f"""
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


def _advanced_section_html(state: dict[str, str]) -> str:
    """Manual stop IDs / lat-lon / routes. Sits behind a `<details>`
    so the median user never sees it; power users + recovery from a
    bad save path can use it without re-doing the address step."""
    lat, lon = "", ""
    coords = _coords(state)
    if coords is not None:
        lat = f"{coords[0]:.3f}"
        lon = f"{coords[1]:.3f}"
    sub_stop = _value_for(state, "JASPER_SUBWAY_STATION_ID")
    bus_stop = _value_for(state, "JASPER_BUS_STOP_ID")
    return f"""
<details class="advanced">
  <summary>Advanced — enter coordinates or stop IDs manually</summary>
  <div class="advanced-body">
    <p class="hint">If you'd rather not geocode an address, paste coordinates from any map app. Three-decimal precision (~110&nbsp;m) is plenty.</p>
    <form method="post" action="geocode" style="margin-bottom:1em">
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

    <label for="adv_bus_stop">Bus stop ID</label>
    <input id="adv_bus_stop" name="nyc_bus_stop" type="text"
           form="save-form"
           placeholder="MTA_302680"
           value="{html.escape(bus_stop)}">
    <small>Accepts either <code>MTA_302680</code> or just <code>302680</code>. Find your stop ID on the BusTime bus-stop sign or at <a href="https://bustime.mta.info/" target="_blank" rel="noopener">bustime.mta.info</a>.</small>
  </div>
</details>"""


def _index_html(state: dict[str, str], *, status_msg: str = "") -> bytes:
    coords = _coords(state)

    if coords is None:
        # No coords yet — only the address section is interactive.
        body = f"""
<p class="sub">Configure NYC subway and bus settings so you can ask "next train" / "next bus" from the speaker.</p>
{_address_section_html(state)}
{_advanced_section_html(state)}"""
        return _wrap_transit_page("Transit", body, status_msg=status_msg)

    providers_covering = transit.covering(*coords)
    if not providers_covering:
        body = f"""
<p class="sub">Configure transit settings.</p>
{_address_section_html(state)}
{_no_coverage_html()}
{_advanced_section_html(state)}"""
        return _wrap_transit_page("Transit", body, status_msg=status_msg)

    cards: list[str] = []
    for p in providers_covering:
        if p.id == "nyc_subway":
            cards.append(_subway_card_html(p, state))
        elif p.id == "nyc_bus":
            cards.append(_bus_card_html(p, state))
        else:
            # Future-proof: any new provider not handled by a dedicated
            # renderer falls back to a placeholder so the page still
            # works — the contributor adding the provider sees what
            # they need to wire next.
            cards.append(f"""
<section class="provider-card">
  <h2>{html.escape(p.label)} <span class="badge muted">no UI yet</span></h2>
  <p class="blurb">This provider is in the registry but doesn't have a wizard card yet. Add one to <code>jasper/web/transit_setup.py</code>.</p>
</section>""")

    body = f"""
<p class="sub">Configure NYC subway and bus settings so you can ask "next train" / "next bus" from the speaker.</p>

{_address_section_html(state)}

<form method="post" action="save" id="save-form">
  <h2>Transit options near you</h2>
  {''.join(cards)}

  <div class="save-row">
    <button type="submit">Save and restart voice</button>
    <span class="hint">Voice picks up the new settings in about 5 seconds.</span>
  </div>
</form>

{_advanced_section_html(state)}

<p class="hint" style="margin-top:2em">
  <form method="post" action="clear" style="display:inline" onsubmit="return confirm('Clear all saved transit settings? Subway and bus tools will stop responding until reconfigured.');">
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

        def _redirect(self, location: str) -> None:
            # Content-Length:0 is load-bearing — see wake_setup.py for
            # the rationale (nginx HTTP/1.0 upstream waits for body
            # otherwise, adding ~5s latency to every redirect).
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(url.query)
            if path == "/":
                state = _load_state(cfg["state_path"])
                self._send_html(_index_html(
                    state, status_msg=qs.get("msg", [""])[0],
                ))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            form = read_form(self)
            if path == "/geocode":
                self._handle_geocode(form)
                return
            if path == "/save":
                self._handle_save(form)
                return
            if path == "/clear":
                self._handle_clear()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def _handle_geocode(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            new, err = _apply_geocode(form, current)
            if err is not None:
                self._redirect(f"./?msg={urllib.parse.quote(err)}")
                return
            try:
                write_env_file(cfg["state_path"], new, mode=TRANSIT_FILE_MODE)
            except OSError as e:
                logger.exception("could not write transit.env after geocode")
                self._redirect(f"./?msg={urllib.parse.quote(f'Could not save: {e}')}")
                return
            display = new.get(DISPLAY_NAME_ENV, "")
            self._redirect(
                f"./?msg={urllib.parse.quote(f'Found location: {display}')}"
            )

        def _handle_save(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            new, err = _apply_save(form, current)
            if err is not None:
                self._redirect(f"./?msg={urllib.parse.quote(err)}")
                return
            try:
                if new:
                    write_env_file(cfg["state_path"], new, mode=TRANSIT_FILE_MODE)
                else:
                    delete_env_file(cfg["state_path"])
            except OSError as e:
                logger.exception("could not write transit.env after save")
                self._redirect(f"./?msg={urllib.parse.quote(f'Could not save: {e}')}")
                return
            restart_voice_daemon()
            self._redirect(
                f"./?msg={urllib.parse.quote('Saved. Voice daemon restarting.')}"
            )

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
                self._redirect(f"./?msg={urllib.parse.quote(f'Could not save: {e}')}")
                return
            restart_voice_daemon()
            self._redirect(
                f"./?msg={urllib.parse.quote('Cleared transit settings. Voice restarting.')}"
            )

    return Handler


# ----------------------------------------------------------------------
# Entry points.
# ----------------------------------------------------------------------


def make_server(target, *, state_path: str = TRANSIT_FILE) -> ThreadingHTTPServer:
    from . import _systemd
    cfg = {"state_path": state_path}
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
