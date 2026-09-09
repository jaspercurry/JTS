# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Page rendering for the /assistant/transit/ wizard."""
from __future__ import annotations

import html
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from .. import google_routes, location_state, transit
from ..bus import parse_bus_stops
from ..secret_redaction import redact_secrets
from ._common import (
    canonical_banner,
    canonical_header,
    canonical_page,
    csrf_field_html,
    mask_secret,
    value_for_env as _value_for,
)

logger = logging.getLogger(__name__)


# Owned by ..location_state / ..google_routes; transit_setup.py imports
# these from here rather than redeclaring them.
LAT_ENV = location_state.TRANSIT_LAT_ENV
LON_ENV = location_state.TRANSIT_LON_ENV
DISPLAY_NAME_ENV = location_state.TRANSIT_DISPLAY_NAME_ENV
TRAVEL_DEFAULT_MODE_ENV = google_routes.TRAVEL_DEFAULT_MODE_ENV
GOOGLE_ROUTES_API_KEY_ENV = google_routes.GOOGLE_ROUTES_API_KEY_ENV


# Max distance (mi) to a nearest stop before we consider the provider
# uncovered. NYC bbox includes some areas (e.g., Sandy Hook NJ tip)
# that are technically in the rectangle but where the nearest subway
# is 10+ mi away — treat those as no-coverage to spare the user a
# misleading card.
MAX_NEAREST_STOP_MILES = 5.0


def _coords(state: dict[str, str]) -> tuple[float, float] | None:
    """Parsed (lat, lon) or None if not geocoded yet."""
    try:
        return (
            float(_value_for(state, LAT_ENV)),
            float(_value_for(state, LON_ENV)),
        )
    except ValueError:
        return None


def _bus_key_source(state: dict[str, str]) -> str:
    """Return where the bus key lives: 'state' / 'env' / 'none'.

    'state' = persisted in /var/lib/jasper/transit.env (the wizard's
              owned file).
    'env'   = visible in os.environ (operator pasted it into
              /etc/jasper/jasper.env directly, OR migrated by
              install.sh into transit.env which systemd re-sourced
              into our env on the next jasper-web spawn).
    'none'  = not set anywhere.

    Used to drive the locked / soft-unlocked / unlocked card states.
    Save decisions read `JASPER_MTA_BUSTIME_KEY` directly off state
    (state-only) — this function is for rendering only."""
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


def _badge_html(configured: bool) -> str:
    """A canonical status badge for a provider card's header."""
    if configured:
        return '<span class="badge badge--ok">configured</span>'
    return '<span class="badge badge--idle">not configured</span>'



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
    back_href: str = "/assistant/",
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
    <span class="badge badge--idle">awaiting address</span>
  </div>
  <p class="provider-card__blurb">Enter your address above to find nearby subway stations.</p>
</section>"""

    stops = provider.find_stops_near(*coords, count=5)
    if not stops or stops[0].distance_mi > MAX_NEAREST_STOP_MILES:
        return f"""
<section class="info-card provider-card">
  <div class="provider-card__head">
    <h2 class="provider-card__title">{html.escape(provider.label)}</h2>
    <span class="badge badge--idle">no stations nearby</span>
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
    <span class="badge badge--idle">awaiting address</span>
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
    <span class="badge badge--warn">needs an API key</span>
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
        # An httpx error repr carries the full URL with ?key=<BusTime key>;
        # scrub before it reaches the log OR the html.escape()-d error
        # banner served on the household LAN.
        safe = redact_secrets(repr(e))
        logger.warning("bus stops fetch raised: %s", safe)
        error = f"unexpected error: {safe}"

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
    masked = mask_secret(saved_key.strip())
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
                cls = (
                    "stop-row stop-row--nested active"
                    if is_active else "stop-row stop-row--nested"
                )
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
<label class="{cls}">
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
    <span class="badge badge--idle">awaiting address</span>
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
        # Citi Bike is keyless (no live secret), but mirror the bus path's
        # scrub discipline so an error repr with any URL is masked
        # consistently in the log and the LAN-served error banner.
        safe = redact_secrets(repr(e))
        logger.warning("citibike stops fetch raised: %s", safe)
        error = f"unexpected error: {safe}"

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

  <p class="eyebrow stations-heading">Stations near you</p>
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
<details class="disclosure advanced-coords">
  <summary>Advanced — enter coordinates or stop IDs manually</summary>
  <div class="disclosure__body">
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
    back_href: str = "/assistant/",
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
    <span class="badge badge--idle">no UI yet</span>
  </div>
  <p class="provider-card__blurb">This provider is in the registry but doesn't have a wizard card yet. Add one to <code>jasper/web/transit_page.py</code>.</p>
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

    # The Clear form's destructive confirm rides in data-confirm, wired by
    # the shared confirm-forms.js module — canonical pages carry no inline
    # dialog helper.
    body = f"""
<p class="form-hint">Configure travel-time directions plus NYC subway and bus settings.</p>

{_address_section_html(state, csrf_token)}

{_cities_section_html(state, csrf_token, coords)}

{save_form}

{_advanced_section_html(state, csrf_token)}

<form method="post" action="clear" id="clear-form" class="clear-form"
      data-confirm="Clear all saved transit settings? Subway and bus tools will stop responding until reconfigured."
      data-confirm-danger="1">
  {csrf_field_html(csrf_token) if csrf_token else ''}
  <button type="submit" class="btn btn--danger">Clear all transit settings</button>
</form>"""
    return _wrap_transit_page(
        "Transit", body, status_msg=status_msg, back_href=back_href,
    )

