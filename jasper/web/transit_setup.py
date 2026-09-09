# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Transit configuration wizard at /assistant/transit/.

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

URL surface (after nginx strips /assistant/transit/):
  GET  /             page render (geocodes once on Submit, never on render)
  POST /geocode      address → coords; redirects back
  POST /save         persist picks; restart voice; redirects back
  POST /cities       persist city-pack on/off toggles; restart voice
  POST /clear        wipe transit config; restart voice; redirects back

Page rendering — the canonical page shell, address/cities sections, and
provider cards — lives in jasper/web/transit_page.py; this module owns
the handler, routes, and save logic.
"""
from __future__ import annotations

import html
import logging
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .. import google_routes, location_state, transit
from ..atomic_io import locked_transform_env_file
from ..transit import geocode as geocode_mod
from ..secret_redaction import redact_secrets
from ..log_event import log_event
from ..env_file import delete_env_file, read_env_file, write_env_file
from ._common import (
    api_key_token_is_valid,
    begin_request,
    reject_csrf,
    send_html_response,
    send_see_other,
    guard_read_request,
    guard_mutating_request,
    read_form,
    restart_voice_daemon,
    safe_back_href,
    SECRET_ENV_MODE,
)
# Rendering helpers resolve names in transit_page's globals: patch
# transit_page.<name>, not these aliases.
from .transit_page import _index_html, _wrap_transit_page

logger = logging.getLogger(__name__)


# Persisted at /var/lib/jasper/transit.env. Mode 0640 — the BusTime
# key is mildly sensitive but not as critical as an OAuth token.
TRANSIT_FILE = location_state.TRANSIT_FILE
TRANSIT_FILE_MODE = location_state.TRANSIT_FILE_MODE
GOOGLE_ROUTES_SECRET_FILE = google_routes.GOOGLE_ROUTES_SECRET_FILE

# Wizard-owned coordinate state (JASPER_TRANSIT_LAT, JASPER_TRANSIT_LON,
# JASPER_TRANSIT_DISPLAY_NAME — written by the geocode handlers below).
# Provider-owned env keys come from `transit.all_env_keys()`. Splitting
# these is deliberate: coords are wizard-internal scaffolding, not
# consumed by daemons directly.
LAT_ENV = location_state.TRANSIT_LAT_ENV
LON_ENV = location_state.TRANSIT_LON_ENV
DISPLAY_NAME_ENV = location_state.TRANSIT_DISPLAY_NAME_ENV
TRAVEL_DEFAULT_MODE_ENV = google_routes.TRAVEL_DEFAULT_MODE_ENV
GOOGLE_ROUTES_API_KEY_ENV = google_routes.GOOGLE_ROUTES_API_KEY_ENV


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


def _locked_apply(state_path: str, current: dict[str, str], new: dict[str, str]) -> None:
    """Serialize a transit.env read-modify-write under the shared flock.

    transit.env has two writers in one jasper-web process: these wizard
    handlers and weather_setup's _seed_transit_from_weather_if_missing. An
    unlocked read-then-whole-file-replace can lose a concurrent write. Rather
    than replay the handler's pre-lock snapshot verbatim, replay only the
    (``current`` -> ``new``) diff onto the file as re-read INSIDE the lock:
    keys ``new`` changed are applied, keys it dropped are removed, and every
    other key — foreign keys, or a key a concurrent writer just set — is
    preserved. Deletes the file when the merged result is empty (parity with
    the old ``write``/``delete_env_file`` branch). Symmetric with the
    weather.env fix (DA-0036) and shares its advisory flock semantics.
    """
    changed = {k: v for k, v in new.items() if current.get(k) != v}
    dropped = [k for k in current if k not in new]

    def _transform(locked: dict[str, str]) -> dict[str, str] | None:
        result = dict(locked)
        for k in dropped:
            result.pop(k, None)
        result.update(changed)
        return result or None

    locked_transform_env_file(state_path, _transform, mode=TRANSIT_FILE_MODE)


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
    # weather.env is also written by weather_setup's own save/clear. Take the
    # shared flock and re-read weather INSIDE the lock: the
    # "weather already has coords → skip" decision and the write must be one
    # atomic step, or a concurrent weather save can be clobbered (or this seed
    # can overwrite coords the user just entered).
    seeded = False

    def _seed_transform(weather_state: dict[str, str]) -> dict[str, str] | None:
        nonlocal seeded
        if location_state.parse_weather_location(weather_state) is not None:
            return weather_state  # already has coords — no-op under the lock
        units = (
            weather_state.get(location_state.WEATHER_UNITS_ENV, "").strip()
            or os.environ.get(location_state.WEATHER_UNITS_ENV, "").strip()
            or None
        )
        new_weather = dict(weather_state)
        new_weather.update(
            location_state.weather_env_for_location(loc, units=units)
        )
        seeded = True
        return new_weather

    locked_transform_env_file(
        weather_path, _seed_transform, mode=location_state.WEATHER_FILE_MODE,
    )
    return seeded


def _validate_google_routes_key(key: str) -> str | None:
    if not key:
        return None
    if any(ch.isspace() for ch in key):
        return "Google Routes API key contains whitespace; copy it again."
    if not api_key_token_is_valid(key):
        return (
            "Google Routes API key contains characters that don't look like "
            "an API key; copy it again."
        )
    return None


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
            # An unanticipated httpx error repr carries the full URL with
            # ?key=<BusTime key>; never let it reach the log.
            logger.warning("bus credential probe raised: %s", redact_secrets(repr(e)))
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
                            default="/assistant/",
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
                _locked_apply(cfg["state_path"], current, new)
                _seed_weather_from_transit_if_missing(
                    new, weather_path=cfg["weather_path"],
                )
            except OSError as e:
                logger.exception("could not write transit.env after geocode")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
            display = new.get(DISPLAY_NAME_ENV, "")
            # Geocoded coordinates and display names reveal the household's
            # location, so record only that a successful mutation landed.
            log_event(logger, "transit.geocode", client=self.address_string())
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
                _locked_apply(cfg["state_path"], current, new)
                if new:
                    _seed_weather_from_transit_if_missing(
                        new, weather_path=cfg["weather_path"],
                    )
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
                _locked_apply(cfg["state_path"], current, new)
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
                _locked_apply(cfg["state_path"], current, new)
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
    from ..platform import systemd
    cfg = {
        "state_path": state_path,
        "routes_secret_path": routes_secret_path,
        "weather_path": weather_path,
    }
    return systemd.make_http_server(target, _make_handler(cfg))
