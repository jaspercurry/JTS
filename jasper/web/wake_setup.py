"""Wake-word picker at /wake/.

One row per curated wake-word model (see jasper/wake_models.py for the
registry). A radio at each row picks which model the voice loop loads
on its next restart. Bundled openWakeWord names ("hey_jarvis", "alexa",
"hey_mycroft") always show as available. Non-bundled models (today:
"jarvis_v2" from the fwartner Home Assistant community collection)
appear with a "not downloaded" hint when their `.onnx` file is missing
on disk — install.sh fetches them on every deploy, so this state is
usually transient (offline install / partial mirror).

Persistence: writes /var/lib/jasper/wake_model.env at mode 0644 with a
single line `JASPER_WAKE_MODEL=...`. The jasper-voice systemd unit
sources this file AFTER /etc/jasper/jasper.env, so wizard-written
values win over operator-managed defaults — same pattern as
/var/lib/jasper/voice_provider.env and spotify_credentials.env. Mode
0644 because the file holds a path, not a secret.

Restart: every successful save kicks `systemctl restart jasper-voice`.
The wake loop is back about 3-4 s later with the new model loaded.

URL surface (after nginx strips the /wake/ prefix):
  GET  /         page render
  POST /save     write wake_model.env + restart voice daemon
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

from .. import wake_models
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


WAKE_MODEL_FILE = wake_models.WAKE_MODEL_FILE


# ----------------------------------------------------------------------
# State helpers — pure where possible.
# ----------------------------------------------------------------------


def _load_state(path: str = WAKE_MODEL_FILE) -> dict[str, str]:
    """Read the wizard-managed env file ({} on missing/blank)."""
    return read_env_file(path)


def _active_model(state: dict[str, str]) -> str:
    """The wake-model string the daemon would actually load right now.

    Order of preference:
      1. wake_model.env (wizard-managed)
      2. process env (systemd already merged /etc/jasper/jasper.env)
      3. compiled-in default ("hey_jarvis")
    """
    val = state.get("JASPER_WAKE_MODEL", "").strip()
    if val:
        return val
    return os.environ.get("JASPER_WAKE_MODEL", "").strip() or "hey_jarvis"


def _is_available(entry: wake_models.WakeModelEntry) -> bool:
    """Bundled openWakeWord names are always considered available — the
    package downloads them lazily on first use and install.sh primes
    that cache. External files have to exist on disk to be loadable;
    a missing file means a failed install-time download (rare, but
    flagged in the UI so the household knows what's going on)."""
    if entry.bundled:
        return True
    return os.path.exists(entry.model)


# ----------------------------------------------------------------------
# HTML rendering.
# ----------------------------------------------------------------------


_WAKE_PAGE_STYLE = PAGE_STYLE + """
  .wake-help { color: #555; font-size: 0.93em; margin: 0.4em 0 1.4em;
               line-height: 1.5; }
  .wake-row {
    display: block; padding: 0.9em 1em;
    background: #f4f4f4; border: 1px solid #e6e6e6; border-radius: 8px;
    margin-bottom: 0.6em; cursor: pointer;
    transition: background 0.15s ease, border-color 0.15s ease;
  }
  .wake-row:hover { background: #f0fff4; border-color: #1db954; }
  .wake-row.active { background: #f0fff4; border-color: #1db954; }
  .wake-row.unavailable { opacity: 0.55; cursor: not-allowed; }
  .wake-row.unavailable:hover { background: #f4f4f4; border-color: #e6e6e6; }
  .wake-row .header {
    display: flex; align-items: center; gap: 0.6em;
    margin-bottom: 0.25em;
  }
  .wake-row input[type=radio] {
    width: auto; flex: none; margin: 0;
  }
  .wake-row .label {
    font-weight: 600; font-size: 1.02em; color: #222; flex: 1;
  }
  .wake-row .badge {
    background: #4a8; color: white; padding: 0.1em 0.55em;
    border-radius: 4px; font-size: 0.78em;
  }
  .wake-row .badge.recommended { background: #1db954; }
  .wake-row .badge.muted { background: #aaa; }
  .wake-row .pronunciation {
    color: #444; font-size: 0.93em; margin: 0.15em 0 0.35em 1.6em;
    font-style: italic;
  }
  .wake-row .description {
    color: #555; font-size: 0.9em; line-height: 1.5;
    margin: 0.2em 0 0 1.6em;
  }
  .wake-row .stats {
    color: #888; font-size: 0.83em; margin: 0.4em 0 0 1.6em;
    font-variant-numeric: tabular-nums;
  }
  .wake-row .stats a {
    color: #888; text-decoration: underline;
  }
  .wake-row .stats a:hover { color: #1db954; }
"""


def _wrap_wake_page(title: str, body: str, *, status_msg: str = "") -> bytes:
    page = wrap_page(title, body, status_msg=status_msg).decode()
    return page.replace(
        f"<style>{PAGE_STYLE}</style>",
        f"<style>{_WAKE_PAGE_STYLE}</style>",
    ).encode()


def _row_html(
    entry: wake_models.WakeModelEntry,
    *,
    is_active: bool,
    available: bool,
) -> str:
    """Render one model row. Disabled state shows a "not downloaded"
    badge instead of "recommended" / "active" so the household can
    tell at a glance why they can't pick it."""
    classes = ["wake-row"]
    if is_active:
        classes.append("active")
    if not available:
        classes.append("unavailable")

    badges = []
    if is_active:
        badges.append('<span class="badge">active</span>')
    if entry.recommended and not is_active:
        badges.append('<span class="badge recommended">recommended</span>')
    if not available:
        badges.append('<span class="badge muted">not downloaded</span>')

    radio_attrs = ['type="radio"', 'name="model"', f'value="{html.escape(entry.key)}"']
    if is_active:
        radio_attrs.append("checked")
    if not available:
        radio_attrs.append("disabled")
    radio = f'<input {" ".join(radio_attrs)}>'

    stats_bits: list[str] = []
    if entry.fa_per_hour is not None:
        stats_bits.append(
            f"~{entry.fa_per_hour:.2f} false fires/hour (author-reported)"
        )
    if entry.bundled:
        stats_bits.append("bundled with openWakeWord")
    else:
        stats_bits.append("downloaded at install time")
    stats_bits.append(
        f'<a href="{html.escape(entry.source_url)}" target="_blank" rel="noopener">source ↗</a>'
    )

    return f"""
<label class="{' '.join(classes)}">
  <div class="header">
    {radio}
    <span class="label">{html.escape(entry.label)}</span>
    {' '.join(badges)}
  </div>
  <div class="pronunciation">{html.escape(entry.pronunciation)}</div>
  <div class="description">{html.escape(entry.description)}</div>
  <div class="stats">{' · '.join(stats_bits)}</div>
</label>"""


def _custom_row_html(model: str, *, is_active: bool) -> str:
    """Operator set JASPER_WAKE_MODEL by hand to something outside the
    curated registry — show it as a non-clickable info row so the
    wizard never silently overwrites their choice. They keep it by
    leaving the radio alone; they replace it by picking a registered
    row and hitting Save."""
    return f"""
<label class="wake-row {'active' if is_active else ''}" style="cursor:default">
  <div class="header">
    <input type="radio" name="model" value="__custom__" checked disabled>
    <span class="label">Custom: {html.escape(model)}</span>
    {'<span class="badge">active</span>' if is_active else ''}
  </div>
  <div class="description">
    Set via <code>JASPER_WAKE_MODEL</code> in
    <code>/etc/jasper/jasper.env</code>. The wizard won't touch this
    unless you pick one of the rows above and hit Save (which writes
    <code>/var/lib/jasper/wake_model.env</code>, layered on top).
  </div>
</label>"""


def _index_html(state: dict[str, str], *, status_msg: str = "") -> bytes:
    active = _active_model(state)
    active_entry = wake_models.by_model(active)
    rows: list[str] = []
    if active_entry is None and active:
        # Custom row at the top so the household sees what's currently
        # in effect before the registered alternatives.
        rows.append(_custom_row_html(active, is_active=True))
    for entry in wake_models.REGISTRY:
        rows.append(_row_html(
            entry,
            is_active=(active_entry is entry),
            available=_is_available(entry),
        ))
    body = f"""
<p class="wake-help">
  Pick which wake phrase the speaker listens for. Models marked
  <em>not downloaded</em> failed their install-time fetch and can be
  retried by re-running <code>bash scripts/deploy-to-pi.sh</code>.
  Saving restarts the voice daemon; it's listening again in about
  4 seconds.
</p>

<form method="post" action="save">
  {''.join(rows)}
  <p style="margin-top:1.4em">
    <button type="submit">Save and restart voice</button>
  </p>
</form>
"""
    return _wrap_wake_page(
        "Wake word", body, status_msg=status_msg,
    )


# ----------------------------------------------------------------------
# Save logic — pure where possible.
# ----------------------------------------------------------------------


def _apply_save(
    form: dict[str, str],
    current: dict[str, str],
) -> tuple[dict[str, str], str | None]:
    """Validate the form selection and produce the new wake_model.env
    state. Returns `(state, error)`; the caller writes the file iff
    error is None."""
    key = (form.get("model") or "").strip()
    if not key:
        return current, "No model selected."
    if key == "__custom__":
        # Defensive — the input is disabled in the rendered form, but a
        # crafted POST could submit it. Reject so we never persist a
        # nonsense token to the env file.
        return current, "The custom row is read-only — pick a registered model."
    entry = wake_models.by_key(key)
    if entry is None:
        return current, f"Unknown model: {key!r}."
    if not _is_available(entry):
        return current, (
            f"{entry.label} isn't downloaded yet on this speaker. "
            "Re-run `bash scripts/deploy-to-pi.sh` to fetch it, then "
            "try again."
        )
    new = dict(current)
    new["JASPER_WAKE_MODEL"] = entry.model
    return new, None


# ----------------------------------------------------------------------
# HTTP handler.
# ----------------------------------------------------------------------


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
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
            if path == "/save":
                self._handle_save(form)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def _handle_save(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            new, err = _apply_save(form, current)
            if err is not None:
                self._redirect(f"./?msg={urllib.parse.quote(err)}")
                return
            try:
                if new:
                    write_env_file(cfg["state_path"], new, mode=0o644)
                else:
                    delete_env_file(cfg["state_path"])
            except OSError as e:
                logger.exception("could not write wake-model env file")
                self._redirect(
                    f"./?msg={urllib.parse.quote(f'Could not save: {e}')}"
                )
                return
            restart_voice_daemon()
            picked = new.get("JASPER_WAKE_MODEL", "")
            entry = wake_models.by_model(picked)
            label = entry.label if entry else picked
            self._redirect(
                f"./?msg={urllib.parse.quote(f'Saved. Voice daemon restarting on {label}.')}"
            )

    return Handler


# ----------------------------------------------------------------------
# Entry points.
# ----------------------------------------------------------------------


def make_server(target, *, state_path: str = WAKE_MODEL_FILE) -> ThreadingHTTPServer:
    from . import _systemd
    cfg = {"state_path": state_path}
    return _systemd.make_http_server(target, _make_handler(cfg))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-wake-web",
        description="Wake-word picker UI for the Jasper smart speaker",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_WAKE_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_WAKE_WEB_PORT", "8774")),
    )
    parser.add_argument(
        "--state", default=os.environ.get("JASPER_WAKE_MODEL_FILE", WAKE_MODEL_FILE),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server((args.host, args.port), state_path=args.state)
    logger.info(
        "jasper-wake-web listening on http://%s:%d (state=%s)",
        args.host, args.port, args.state,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
