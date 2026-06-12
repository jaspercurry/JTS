"""AirPlay sync mode toggle at /airplay/.

Single setting: synced (default, glitch-free since the resync_threshold
bump in shairport-sync.conf.template) vs free-running (fallback for
unforeseen DAC-specific issues).

History: shairport-sync's drift-correction logic on this chain
(shairport → snd-aloop → CamillaDSP → dmix → USB DAC) was firing a
discrete tear-the-audio path every ~63s because `snd_pcm_delay()` on
the loopback handle reports ring fill, not actual DAC latency
(mikebrady/shairport-sync#1980). Initial mitigation was
`disable_synchronization=yes` ("free-running" mode) which we shipped
as the default. Later we found the proper fix: raise
`resync_threshold_in_seconds` to 0.2 so shairport stays in its
continuous ±1-sample stuffing path (smooth, inaudible) and never
fires the discrete path. With that change in place, synced mode is
glitch-free on this hardware. See
docs/HANDOFF-airplay.md for the full diagnosis.

This toggle remains as a safety net: if a future DAC swap or
firmware change produces sync issues we don't currently see, the
user can flip to free-running. For typical use, synced is correct.

Persistence: /var/lib/jasper/airplay_mode.env, key
`JASPER_AIRPLAY_FREE_RUNNING=yes|no`. The shairport-sync.service
ExecStartPre re-renders /etc/shairport-sync.conf from the template
on every start, so any restart picks up the current setting.

URL surface (after nginx strips /airplay/):
  GET  /        page render
  POST /save    write mode, restart shairport-sync
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ._common import (
    begin_request,
    canonical_banner,
    canonical_header,
    canonical_page,
    csrf_field_html,
    read_env_file,
    read_form,
    reject_csrf,
    send_html_response,
    send_see_other,
    guard_read_request,
    guard_mutating_request,
    write_env_file,
)

logger = logging.getLogger(__name__)


MODE_FILE = "/var/lib/jasper/airplay_mode.env"
ENV_VAR = "JASPER_AIRPLAY_FREE_RUNNING"


def _current_mode(path: str = MODE_FILE) -> str:
    """Return 'free-running' or 'synced'. Default 'synced' when the
    env file is missing or holds an unrecognized value — synced is
    glitch-free on this chain since the resync_threshold fix and is
    the right default for video A/V sync + multi-room AirPlay."""
    env = read_env_file(path)
    val = env.get(ENV_VAR, "no").strip().lower()
    if val in ("no", "false", "0"):
        return "synced"
    return "free-running"


def _apply_save(form: dict[str, str]) -> tuple[str | None, str | None]:
    """Validate the submitted mode and return (mode-to-persist, error)."""
    mode = (form.get("mode") or "").strip()
    if mode not in ("free-running", "synced"):
        return None, f"Unknown mode {mode!r}."
    return mode, None


def _restart_shairport() -> None:
    """Restart shairport-sync so its ExecStartPre re-renders
    /etc/shairport-sync.conf from the template + current env file.
    Best-effort — log but don't raise."""
    try:
        subprocess.run(
            ["systemctl", "restart", "shairport-sync"],
            check=False, timeout=15,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("shairport-sync restart failed: %s", e)


def _index_html(mode: str, csrf_token: str, *, status_msg: str = "") -> bytes:
    """Render the mode page. Two radio options with use-case copy.

    Restyled onto the canonical design system: the document shell comes
    from ``canonical_page`` (links /assets/app.css), the top bar from
    ``canonical_header``, and any flash from ``canonical_banner``. The
    mode choice stays a server-rendered radio group (``_apply_save``
    reads ``form["mode"]``); each option is a ``.choice`` card styled by
    the page stylesheet. There is no client JS — the form POSTs to
    ./save like every other wizard."""
    fr_checked = "checked" if mode == "free-running" else ""
    sy_checked = "checked" if mode == "synced" else ""
    body = f"""
{canonical_header("AirPlay sync mode")}
<main class="page">
  {canonical_banner(status_msg)}
  <p class="form-hint">Controls how the AirPlay receiver handles clock drift
  between your sender (Mac, iPhone, iPad) and the speaker. The default works
  for everything — this setting is here as a safety net.</p>

  <form method="post" action="./save">
    {csrf_field_html(csrf_token)}
    <label class="choice">
      <input type="radio" name="mode" value="synced" {sy_checked}>
      <span class="choice__body">
        <span class="choice__title">Synced <span class="choice__tag">default</span></span>
        <span class="choice__hint">Tracks the AirPlay sender's clock. Music plays
        cleanly, video A/V sync is preserved, and multi-room AirPlay with other
        speakers stays in sync. Recommended unless you're hitting a DAC-specific
        issue.</span>
      </span>
    </label>
    <label class="choice">
      <input type="radio" name="mode" value="free-running" {fr_checked}>
      <span class="choice__body">
        <span class="choice__title">Free-running <span class="choice__tag">fallback</span></span>
        <span class="choice__hint">Plays audio without tracking the sender's clock.
        Use this only if you've swapped to a USB DAC that exhibits periodic glitches
        in synced mode (very-low-quality crystals can exceed shairport's
        continuous-correction headroom). Tradeoffs: video A/V sync drifts over
        multi-hour sessions; multi-room AirPlay drifts between speakers.</span>
      </span>
    </label>
    <div class="form-actions">
      <button type="submit" class="btn btn--primary">Save and restart AirPlay</button>
    </div>
  </form>

  <details class="disclosure">
    <summary>Why this knob exists</summary>
    <div class="disclosure__body">
      <p>The JTS audio chain runs shairport-sync → snd-aloop →
      CamillaDSP → dmix → USB DAC. Earlier in the project, shairport's
      drift correction periodically misfired on this chain — its
      <code>snd_pcm_delay()</code> reads the loopback ring fill instead
      of the actual DAC latency, so a slow clock-drift between the host
      and the dongle would trigger a discrete "tear" every ~60 s
      (confirmed against
      <a href="https://github.com/mikebrady/shairport-sync/issues/1980">
      mikebrady/shairport-sync#1980</a>).</p>
      <p>We fixed it by raising shairport's
      <code>resync_threshold_in_seconds</code> to 0.2 in the conf
      template, which keeps it in the continuous ±1-sample-stuffing
      path that smoothly absorbs ~667 ppm of drift. Synced mode is now
      glitch-free on this hardware. The setting remains in case a
      future DAC swap produces drift beyond the continuous path's
      headroom (~2500 ppm) — flip to free-running and the speaker plays
      happily without trying to sync.</p>
      <p>Setting persists across reboots in
      <code>/var/lib/jasper/airplay_mode.env</code>. CLI:
      <code>jasper-airplay-mode set [synced|free-running]</code>. Full
      history in <code>docs/HANDOFF-airplay.md</code>.</p>
    </div>
  </details>
</main>
"""
    return canonical_page(
        "AirPlay sync mode",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/airplay/airplay.css",
    )


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                send_html_response(self, _index_html(
                    _current_mode(cfg["state_path"]),
                    ctx["csrf_token"],
                    status_msg=ctx["flash"],
                ))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path != "/save":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            form = read_form(self)
            if not guard_mutating_request(self, form):
                reject_csrf(self)
                return
            if path == "/save":
                mode, err = _apply_save(form)
                if err is not None:
                    send_see_other(self, "./", flash=err)
                    return
                value = "yes" if mode == "free-running" else "no"
                try:
                    write_env_file(
                        cfg["state_path"], {ENV_VAR: value}, mode=0o644,
                    )
                except OSError as e:
                    logger.exception("could not write airplay mode env file")
                    send_see_other(self, "./", flash=f"Could not save: {e}")
                    return
                _restart_shairport()
                send_see_other(
                    self, "./",
                    flash=f"Saved. AirPlay now in {mode} mode (shairport-sync restarted).",
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def make_server(target, *, state_path: str = MODE_FILE) -> ThreadingHTTPServer:
    """Used by jasper.web.__main__ to colocate this server with the
    other settings wizards inside one process. `target` is a
    socket/tuple/int per _systemd.make_http_server's contract."""
    from . import _systemd
    cfg = {"state_path": state_path}
    return _systemd.make_http_server(target, _make_handler(cfg))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-airplay-web",
        description="AirPlay sync-mode toggle UI for the Jasper smart speaker",
    )
    parser.add_argument("--host", default=os.environ.get("JASPER_AIRPLAY_WEB_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_AIRPLAY_WEB_PORT", "8771")),
    )
    parser.add_argument("--state", default=os.environ.get("JASPER_AIRPLAY_MODE_FILE", MODE_FILE))
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server((args.host, args.port), state_path=args.state)
    logger.info("jasper-airplay-web listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
