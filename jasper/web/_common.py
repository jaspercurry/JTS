"""Shared helpers for the JTS web setup pages.

The Spotify-account wizard at `/spotify/` and the voice-provider config
at `/voice/` both run inside the same `jasper-web` daemon (separate
ports, one per nginx route). They share the page CSS, the
HTML-wrapping helper, the systemd-EnvironmentFile read/write atomics,
and the `systemctl restart jasper-voice` shell-out — keeping all of
that here means a styling tweak ripples through every settings page
without two-place edits, and any future settings page (Wi-Fi setup,
diagnostics) plugs in by importing from here.

What's NOT shared: route handlers, page layouts, form bodies. Each
wizard owns its own UX.
"""
from __future__ import annotations

import html
import logging
import os
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler

logger = logging.getLogger(__name__)


# Page CSS. Same look as the Spotify wizard so the speaker presents one
# coherent settings UI instead of a stack of mismatched tools. Spotify-
# green primary button (#1db954) is intentional even on non-Spotify
# pages — the user knows that "the JTS settings green" means "save /
# proceed" by the time they see the second wizard.
PAGE_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         max-width: 620px; margin: 2em auto; padding: 0 1em; color: #222; }
  h1 { margin-bottom: 0.25em; } h2 { margin-top: 2em; }
  .sub { color: #666; margin-top: 0; }
  .msg { background: #e8f4ff; border: 1px solid #abd; padding: 0.6em 0.8em;
          border-radius: 6px; margin: 1em 0; }
  .err { background: #ffe8e8; border-color: #d99; }
  ol.steps { padding-left: 1.4em; }
  ol.steps > li { margin-bottom: 1em; }
  form { margin-top: 1em; }
  label { display: block; margin: 0.6em 0 0.2em; font-weight: 600; }
  input[type=text], input[type=password], select {
    width: 100%; padding: 0.5em; border: 1px solid #bbb;
    border-radius: 4px; font-size: 1em; box-sizing: border-box;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    background: #fff;
  }
  small { color: #666; }
  .hint { color: #666; font-size: 0.92em; }
  button, a.btn {
    background: #1db954; color: white; border: 0;
    padding: 0.6em 1.2em; border-radius: 4px; font-size: 1em;
    cursor: pointer; text-decoration: none; display: inline-block;
  }
  a.btn.secondary, button.secondary { background: #4a4a4a; }
  button.danger { background: #d44; }
  button:hover, a.btn:hover { filter: brightness(1.1); }
  button:disabled { background: #b8b8b8; cursor: not-allowed; filter: none; }
  .copy-row { display: flex; gap: 0.5em; align-items: stretch; margin: 0.6em 0; }
  .copy-row input { flex: 1; }
  .copy-row button { padding: 0 1em; }
  .copy-feedback { color: #1db954; font-weight: 600; margin-left: 0.4em;
                   visibility: hidden; transition: opacity 0.2s; }
  .copy-feedback.shown { visibility: visible; }
  .credbox {
    background: #fafafa; border: 1px solid #ddd; padding: 0.4em 0.8em;
    border-radius: 6px; margin: 0.6em 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.92em; color: #555; word-break: break-all;
  }

  /* Account = expand/collapse card. Used by both /spotify (account
     management) and /voice (per-provider config). Common shape, same
     CSS, lives here once. */
  .accounts-help { color: #666; font-size: 0.92em; margin: 0 0 0.8em; }
  details.account { background: #f4f4f4; border-radius: 6px;
                     margin-bottom: 0.5em; overflow: hidden; }
  details.account > summary {
    list-style: none; cursor: pointer; padding: 0.7em 0.9em;
    display: flex; align-items: center; gap: 0.6em;
    user-select: none; -webkit-user-select: none;
  }
  details.account > summary::-webkit-details-marker { display: none; }
  details.account > summary::before {
    content: "▸"; color: #888; font-size: 0.9em;
    transition: transform 0.15s ease; display: inline-block; width: 0.9em;
  }
  details.account[open] > summary::before { transform: rotate(90deg); }
  details.account > summary:hover { background: #ececec; }
  details.account > summary .name { font-weight: 600; flex: 1; }
  details.account > summary .badge {
    background: #4a8; color: white; padding: 0.1em 0.5em;
    border-radius: 4px; font-size: 0.8em;
  }
  details.account > summary .badge.muted {
    background: #aaa;
  }
  details.account > summary .pl-count {
    color: #888; font-size: 0.88em; font-variant-numeric: tabular-nums;
  }
  details.account .account-body {
    padding: 0 0.9em 0.9em; border-top: 1px solid #e6e6e6;
  }
"""


def wrap_page(title: str, body: str, *, status_msg: str = "") -> bytes:
    """Wrap a body fragment into a complete HTML5 document with the
    shared style and an optional status banner.

    `status_msg` is rendered with an `err` class when it contains
    "error" or "fail" (case-insensitive), otherwise as info."""
    msg_class = "msg err" if (
        "error" in status_msg.lower() or "fail" in status_msg.lower()
    ) else "msg"
    msg_html = (
        f'<p class="{msg_class}">{html.escape(status_msg)}</p>'
        if status_msg else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
{msg_html}
{body}
</body>
</html>""".encode()


def read_env_file(path: str) -> dict[str, str]:
    """Parse a systemd-style EnvironmentFile (KEY=VALUE per line, no
    quoting). Returns {} if the file is missing or unreadable.

    Same shape used by `/var/lib/jasper/spotify_credentials.env` and
    `/var/lib/jasper/voice_provider.env` — both are sourced into
    jasper-voice's environment via systemd's `EnvironmentFile=`."""
    out: dict[str, str] = {}
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("could not read %s: %s", path, e)
    return out


def write_env_file(path: str, values: dict[str, str], *, mode: int = 0o600) -> None:
    """Atomically write a systemd EnvironmentFile-shaped key=value file
    with the given mode (default 0o600 — these files contain API keys
    and OAuth secrets).

    Atomicity matters: a half-written env file at restart time could
    leave jasper-voice with a partial config and a real-world impact
    (silent failure cue, lost session). The temp-file + rename pattern
    here gives the kernel an all-or-nothing swap."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            for key, val in values.items():
                # We write KEY=VALUE without quoting, matching systemd's
                # EnvironmentFile parsing: leading/trailing whitespace
                # in the value is stripped, but no escaping is applied
                # to embedded characters. API keys are alphanumeric
                # with `-`/`_`/`.` so this is safe; an explicit guard
                # keeps that contract honest if someone passes a value
                # with newlines or `=`.
                if "\n" in val or "\r" in val:
                    raise ValueError(f"env value for {key} contains newline")
                f.write(f"{key}={val}\n")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def delete_env_file(path: str) -> None:
    """Best-effort delete; missing-file is fine."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("could not delete %s: %s", path, e)


def restart_voice_daemon() -> None:
    """Best-effort restart of jasper-voice so it picks up new
    credentials / new provider on its next boot. Logs but does not
    raise — the user can always restart by hand if this fails."""
    try:
        subprocess.run(
            ["systemctl", "restart", "jasper-voice"],
            check=False, timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("jasper-voice restart failed: %s", e)


def read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    """Parse a urlencoded form body off a stdlib BaseHTTPRequestHandler
    request into a single-value dict. Empty values are preserved (so
    we can detect "user pasted nothing" vs "field absent")."""
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length).decode("utf-8") if length else ""
    return {
        k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()
    }


def mask_secret(value: str) -> str:
    """Render a secret as `prefix…suffix` for display.

    Always shows enough of the prefix that the user can verify they
    pasted the right key family (sk-… for OpenAI, AIzaSy… for Google,
    xai-… for xAI), but hides the bulk so a screenshot of the page
    doesn't leak the secret. Empty input returns an empty string so
    the caller can render a "(not set)" placeholder."""
    if not value:
        return ""
    if len(value) <= 8:
        return "…" * len(value)
    return f"{value[:4]}…{value[-4:]}"
