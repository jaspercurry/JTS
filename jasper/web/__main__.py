"""Entry point for the jasper-web systemd unit.

Starts every setup wizard in a single process — one ThreadingHTTPServer
per nginx route. They share /var/lib/jasper as their persistence
volume and shell out to systemctl together when something changes,
so colocating them costs nothing extra and saves a separate systemd
unit per wizard. nginx routes:

  /spotify/  →  127.0.0.1:8765  (jasper.web.spotify_setup)
  /voice/    →  127.0.0.1:8767  (jasper.web.voice_setup)
  /google/   →  127.0.0.1:8768  (jasper.web.google_setup)

If any server fails to bind (port collision, permission), the process
exits non-zero so systemd restarts the unit. We don't try to keep some
servers alive while one is down — the user-visible symptom of a
partial start would be a 502 on the broken page, which is more
confusing than 'the whole settings host is restarting'.
"""
from __future__ import annotations

import logging
import os
import threading

from . import airplay_setup, google_setup, spotify_setup, voice_setup

logger = logging.getLogger(__name__)


def _serve_voice(host: str, port: int, state_path: str) -> None:
    """Run the voice-provider wizard server forever. Called in a worker
    thread so the main thread can run the Spotify server (which is the
    older, busier server — the OAuth callback path matters more for
    responsiveness than the rarely-touched provider config)."""
    server = voice_setup.make_server(host, port, state_path=state_path)
    logger.info(
        "jasper-web /voice listening on http://%s:%d (state=%s)",
        host, port, state_path,
    )
    server.serve_forever()


def _serve_google(host: str, port: int, registry_path: str, redirect_uri: str) -> None:
    server = google_setup.make_server(
        host, port, registry_path=registry_path, redirect_uri=redirect_uri,
    )
    logger.info(
        "jasper-web /google listening on http://%s:%d (registry=%s)",
        host, port, registry_path,
    )
    server.serve_forever()


def _serve_airplay(host: str, port: int, state_path: str) -> None:
    server = airplay_setup.make_server(host, port, state_path=state_path)
    logger.info(
        "jasper-web /airplay listening on http://%s:%d (state=%s)",
        host, port, state_path,
    )
    server.serve_forever()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Voice-wizard server — starts in a worker thread so the existing
    # spotify_setup.main() can keep its current "blocks on serve_forever"
    # shape and remain the system-test path's reference flow.
    voice_host = os.environ.get("JASPER_VOICE_WEB_HOST", "127.0.0.1")
    voice_port = int(os.environ.get("JASPER_VOICE_WEB_PORT", "8767"))
    voice_state = os.environ.get(
        "JASPER_VOICE_PROVIDER_FILE", voice_setup.PROVIDER_FILE,
    )
    voice_thread = threading.Thread(
        target=_serve_voice,
        args=(voice_host, voice_port, voice_state),
        name="jasper-web-voice",
        daemon=True,
    )
    voice_thread.start()

    # Google-wizard server — same shape as voice, on its own thread.
    google_host = os.environ.get("JASPER_GOOGLE_WEB_HOST", "127.0.0.1")
    google_port = int(os.environ.get("JASPER_GOOGLE_WEB_PORT", "8768"))
    google_registry = os.environ.get(
        "JASPER_GOOGLE_ACCOUNTS_PATH",
        "/var/lib/jasper/google/accounts.json",
    )
    google_redirect = os.environ.get(
        "GOOGLE_REDIRECT_URI", google_setup.default_redirect_uri(),
    )
    google_thread = threading.Thread(
        target=_serve_google,
        args=(google_host, google_port, google_registry, google_redirect),
        name="jasper-web-google",
        daemon=True,
    )
    google_thread.start()

    # AirPlay sync-mode toggle — same shape as voice/google, own thread.
    airplay_host = os.environ.get("JASPER_AIRPLAY_WEB_HOST", "127.0.0.1")
    airplay_port = int(os.environ.get("JASPER_AIRPLAY_WEB_PORT", "8771"))
    airplay_state = os.environ.get(
        "JASPER_AIRPLAY_MODE_FILE", airplay_setup.MODE_FILE,
    )
    airplay_thread = threading.Thread(
        target=_serve_airplay,
        args=(airplay_host, airplay_port, airplay_state),
        name="jasper-web-airplay",
        daemon=True,
    )
    airplay_thread.start()

    # Spotify wizard server runs on the main thread (blocking call).
    # When systemd sends SIGTERM, spotify_setup.main()'s
    # KeyboardInterrupt path returns 0 and the process exits, which
    # also terminates the daemon worker threads cleanly.
    return spotify_setup.main()


if __name__ == "__main__":
    raise SystemExit(main())
