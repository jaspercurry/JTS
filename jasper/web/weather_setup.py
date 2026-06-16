"""Weather default-location wizard at /weather/.

The weather tool has two location paths:

* Bare weather questions use the wizard-owned default in
  /var/lib/jasper/weather.env.
* Questions that name a city/place are resolved dynamically by
  jasper.weather through Open-Meteo geocoding.

This page owns only the bare-question default and units. It stores
rounded coordinates (same privacy posture as /transit/) plus a display
label; the raw address typed into the form is never persisted.

Migrated to the canonical design system: renders through
``canonical_page`` with the page-specific CSS in
``/assets/weather/weather.css``. It is a plain server-rendered
request/response form, so it ships no ES module.
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import location_state
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
    read_form,
    reject_csrf,
    restart_voice_daemon,
    safe_back_href,
    send_html_response,
    send_see_other,
    guard_read_request,
    guard_mutating_request,
    write_env_file,
)

logger = logging.getLogger(__name__)


WEATHER_FILE = location_state.WEATHER_FILE
WEATHER_FILE_MODE = location_state.WEATHER_FILE_MODE
LAT_ENV = location_state.WEATHER_LAT_ENV
LON_ENV = location_state.WEATHER_LON_ENV
DISPLAY_NAME_ENV = location_state.WEATHER_DISPLAY_NAME_ENV
DEFAULT_LOCATION_ENV = location_state.WEATHER_DEFAULT_LOCATION_ENV
UNITS_ENV = location_state.WEATHER_UNITS_ENV

TRANSIT_FILE = location_state.TRANSIT_FILE

VALID_UNITS = {"celsius", "fahrenheit"}


def _owned_env_keys() -> set[str]:
    return {
        LAT_ENV,
        LON_ENV,
        DISPLAY_NAME_ENV,
        DEFAULT_LOCATION_ENV,
        UNITS_ENV,
    }


def _load_state(path: str = WEATHER_FILE) -> dict[str, str]:
    return read_env_file(path)


def _value_for(state: dict[str, str], env_var: str, default: str = "") -> str:
    val = state.get(env_var, "").strip()
    if val:
        return val
    return os.environ.get(env_var, "") or default


def _units(state: dict[str, str]) -> str:
    value = _value_for(state, UNITS_ENV, "celsius").strip().lower()
    return value if value in VALID_UNITS else "celsius"


def _weather_location(state: dict[str, str]) -> location_state.SavedLocation | None:
    loc = location_state.parse_weather_location(state)
    if loc is not None:
        return loc
    env_state = {
        LAT_ENV: os.environ.get(LAT_ENV, ""),
        LON_ENV: os.environ.get(LON_ENV, ""),
        DISPLAY_NAME_ENV: os.environ.get(DISPLAY_NAME_ENV, ""),
    }
    return location_state.parse_weather_location(env_state)


def _transit_location(state: dict[str, str]) -> location_state.SavedLocation | None:
    loc = location_state.parse_transit_location(state)
    if loc is not None:
        return loc
    env_state = {
        location_state.TRANSIT_LAT_ENV: os.environ.get(
            location_state.TRANSIT_LAT_ENV, "",
        ),
        location_state.TRANSIT_LON_ENV: os.environ.get(
            location_state.TRANSIT_LON_ENV, "",
        ),
        location_state.TRANSIT_DISPLAY_NAME_ENV: os.environ.get(
            location_state.TRANSIT_DISPLAY_NAME_ENV, "",
        ),
    }
    return location_state.parse_transit_location(env_state)


def _seed_transit_from_weather_if_missing(
    weather_state: dict[str, str],
    *,
    transit_path: str = TRANSIT_FILE,
) -> bool:
    loc = location_state.parse_weather_location(weather_state)
    if loc is None:
        return False
    transit_state = read_env_file(transit_path)
    if location_state.parse_transit_location(transit_state) is not None:
        return False
    new_transit = dict(transit_state)
    new_transit.update(location_state.transit_env_for_location(loc))
    write_env_file(
        transit_path,
        new_transit,
        mode=location_state.TRANSIT_FILE_MODE,
    )
    return True


def _state_without_owned_keys(current: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in current.items() if k not in _owned_env_keys()}


def _location_from_manual(form: dict[str, str]) -> tuple[location_state.SavedLocation | None, str | None]:
    manual_lat = (form.get("manual_lat") or "").strip()
    manual_lon = (form.get("manual_lon") or "").strip()
    if not (manual_lat or manual_lon):
        return None, None
    if not (manual_lat and manual_lon):
        return None, "Enter both latitude and longitude, or use the location field."
    try:
        lat = geocode_mod.round_coord(float(manual_lat))
        lon = geocode_mod.round_coord(float(manual_lon))
    except ValueError:
        return None, "Latitude and longitude must be numbers."
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, "Latitude must be -90..90 and longitude -180..180."
    return location_state.SavedLocation(
        lat=lat,
        lon=lon,
        display_name=f"Manual: {lat:.3f}, {lon:.3f}",
    ), None


def _location_from_address(address: str) -> tuple[location_state.SavedLocation | None, str | None]:
    if not address:
        return None, None
    try:
        result = geocode_mod.geocode(address)
    except geocode_mod.GeocodeError as e:
        return None, f"Couldn't geocode that — {e}"
    return location_state.SavedLocation(
        lat=geocode_mod.round_coord(result.lat),
        lon=geocode_mod.round_coord(result.lon),
        display_name=result.display_name,
    ), None


def _apply_save(
    form: dict[str, str],
    current: dict[str, str],
    *,
    transit_state: dict[str, str] | None = None,
) -> tuple[dict[str, str], str | None]:
    units = (form.get("units") or _units(current)).strip().lower()
    if units not in VALID_UNITS:
        return current, "Choose Celsius or Fahrenheit."

    manual_loc, err = _location_from_manual(form)
    if err is not None:
        return current, err
    loc = manual_loc
    if loc is None:
        loc, err = _location_from_address((form.get("location") or "").strip())
        if err is not None:
            return current, err
    if loc is None:
        loc = _weather_location(current)
    if loc is None and transit_state is not None:
        loc = location_state.parse_transit_location(transit_state)

    new = dict(current)
    for key in _owned_env_keys():
        new.pop(key, None)
    if loc is not None:
        new.update(location_state.weather_env_for_location(loc, units=units))
    else:
        legacy = _value_for(current, DEFAULT_LOCATION_ENV).strip()
        if not legacy:
            return current, "Enter a location first."
        new[DEFAULT_LOCATION_ENV] = legacy
        new[UNITS_ENV] = units
    return new, None


WEATHER_PAGE_CSS_HREF = "/assets/weather/weather.css"


def _current_location_html(
    weather_state: dict[str, str],
    transit_state: dict[str, str],
) -> str:
    loc = _weather_location(weather_state)
    source = "weather"
    if loc is None:
        loc = _transit_location(transit_state)
        source = "transit"

    if loc is not None:
        if source == "weather":
            source_text = "Saved weather location."
            tone = "var(--status-ok)"
            badge = "Saved"
        else:
            source_text = (
                "Using the transit location until a "
                "weather-specific location is saved."
            )
            tone = "var(--status-idle)"
            badge = "From transit"
        display = loc.display_name or "(saved location)"
        return f"""
<div class="info-card info-card--accent" style="--tone: {tone};">
  <div class="loc-head">
    <strong class="loc-name">{html.escape(display)}</strong>
    <span class="badge">{badge}</span>
  </div>
  <p class="loc-coords">{loc.lat:.3f}, {loc.lon:.3f} (~110&nbsp;m precision)</p>
  <p class="info-card__hint">{html.escape(source_text)}</p>
</div>"""

    legacy = _value_for(weather_state, DEFAULT_LOCATION_ENV).strip()
    if legacy:
        return f"""
<div class="info-card info-card--accent" style="--tone: var(--status-warn);">
  <div class="loc-head">
    <strong class="loc-name">{html.escape(legacy)}</strong>
    <span class="badge">Legacy</span>
  </div>
  <p class="info-card__hint">Legacy place-name default. Save this page to
  store rounded coordinates for faster, more reliable bare weather
  questions.</p>
</div>"""
    return '<div class="info-card"><p class="info-card__note">No weather default is set yet.</p></div>'


def _units_options_html(active: str) -> str:
    labels = {"celsius": "Celsius", "fahrenheit": "Fahrenheit"}
    return "".join(
        f'<option value="{value}"'
        + (" selected" if value == active else "")
        + f'>{label}</option>'
        for value, label in labels.items()
    )


def _index_html(
    weather_state: dict[str, str],
    transit_state: dict[str, str],
    csrf_token: str,
    *,
    status_msg: str = "",
    back_href: str = "/",
) -> bytes:
    csrf = csrf_field_html(csrf_token)
    current_html = _current_location_html(weather_state, transit_state)
    units_options = _units_options_html(_units(weather_state))
    body = f"""
{canonical_header("Weather", back_href=back_href)}
<main class="page">
  {canonical_banner(status_msg)}
  <p class="form-hint">Default location used when the user asks for the
  weather without naming a place.</p>

  <section class="section">
    <div class="section__head"><h2 class="section__title">Current default</h2></div>
    {current_html}
  </section>

  <form method="post" action="./save">
    {csrf}
    <section class="section">
      <div class="section__head">
        <h2 class="section__title">Set weather location</h2>
      </div>

      <div class="field">
        <label for="location">Location</label>
        <input id="location" name="location" type="text"
               placeholder="Tampa, FL or 123 Main St, Brooklyn NY"
               autocomplete="street-address">
        <p class="form-hint">
          Sent to <a href="https://nominatim.openstreetmap.org/" target="_blank" rel="noopener">OpenStreetMap (Nominatim)</a>
          to look up coordinates. Only rounded coordinates and the display
          label are saved on this speaker.
          <a href="https://operations.osmfoundation.org/policies/nominatim/" target="_blank" rel="noopener">Policy &#8599;</a>
        </p>
      </div>

      <div class="field">
        <label for="units">Temperature units</label>
        <select id="units" name="units">
          {units_options}
        </select>
      </div>

      <details class="advanced">
        <summary>Manual coordinates</summary>
        <div class="advanced-body">
          <div class="field">
            <label for="manual_lat">Latitude</label>
            <input id="manual_lat" name="manual_lat" type="text"
                   inputmode="decimal" placeholder="40.653">
          </div>
          <div class="field">
            <label for="manual_lon">Longitude</label>
            <input id="manual_lon" name="manual_lon" type="text"
                   inputmode="decimal" placeholder="-74.007">
          </div>
          <p class="form-hint">Manual coordinates bypass geocoding.</p>
        </div>
      </details>

      <div class="form-actions">
        <button type="submit" class="btn btn--primary">Save</button>
      </div>
    </section>
  </form>

  <form method="post" action="./clear" class="clear-form">
    {csrf}
    <div class="form-actions">
      <button type="submit" class="btn btn--danger">Clear weather default</button>
    </div>
  </form>
</main>
"""
    return canonical_page(
        "Weather",
        body,
        csrf_token=csrf_token,
        page_css_href=WEATHER_PAGE_CSS_HREF,
    )


def _make_handler(cfg: dict[str, str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: D401
            logger.info("%s - " + fmt, self.address_string(), *args)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(url.query)
            if path == "/":
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                weather_state = _load_state(cfg["state_path"])
                transit_state = read_env_file(cfg["transit_path"])
                body = _index_html(
                    weather_state,
                    transit_state,
                    ctx["csrf_token"],
                    status_msg=ctx["flash"],
                    back_href=safe_back_href((qs.get("return_to") or [""])[0]),
                )
                send_html_response(self, body)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path not in ("/save", "/clear"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            form = read_form(self)
            if not guard_mutating_request(self, form):
                reject_csrf(self)
                return
            if path == "/save":
                self._handle_save(form)
                return
            if path == "/clear":
                self._handle_clear()
                return

        def _handle_save(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            transit_state = read_env_file(cfg["transit_path"])
            new, err = _apply_save(form, current, transit_state=transit_state)
            if err is not None:
                send_see_other(self, "./", flash=err)
                return
            try:
                write_env_file(cfg["state_path"], new, mode=WEATHER_FILE_MODE)
                _seed_transit_from_weather_if_missing(
                    new, transit_path=cfg["transit_path"],
                )
            except OSError as e:
                logger.exception("could not write weather.env")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
            restart_voice_daemon()
            # No coords in the log — they're the household's home location.
            log_event(logger, "weather.save", client=self.address_string())
            send_see_other(self, "./", flash="Saved. Voice daemon restarting.")

        def _handle_clear(self) -> None:
            current = _load_state(cfg["state_path"])
            new = _state_without_owned_keys(current)
            try:
                if new:
                    write_env_file(cfg["state_path"], new, mode=WEATHER_FILE_MODE)
                else:
                    delete_env_file(cfg["state_path"])
            except OSError as e:
                logger.exception("could not clear weather.env")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
            restart_voice_daemon()
            log_event(logger, "weather.clear", client=self.address_string())
            send_see_other(
                self, "./",
                flash="Cleared weather default. Voice restarting.",
            )

    return Handler


def make_server(
    target,
    *,
    state_path: str = WEATHER_FILE,
    transit_path: str = TRANSIT_FILE,
) -> ThreadingHTTPServer:
    from . import _systemd
    cfg = {"state_path": state_path, "transit_path": transit_path}
    return _systemd.make_http_server(target, _make_handler(cfg))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the JTS weather wizard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8779)
    parser.add_argument("--state-path", default=WEATHER_FILE)
    parser.add_argument("--transit-path", default=TRANSIT_FILE)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    server = make_server(
        (args.host, args.port),
        state_path=args.state_path,
        transit_path=args.transit_path,
    )
    logger.info("weather wizard listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
