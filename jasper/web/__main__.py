"""Entry point for the jasper-web systemd unit.

Starts every setup wizard in a single process — one ThreadingHTTPServer
per nginx route. They share /var/lib/jasper as their persistence
volume and shell out to systemctl together when something changes,
so colocating them costs nothing extra and saves a separate systemd
unit per wizard. nginx routes:

  /spotify/  →  127.0.0.1:8765  (jasper.web.spotify_setup)
  /voice/    →  127.0.0.1:8767  (jasper.web.voice_setup)
  /google/   →  127.0.0.1:8768  (jasper.web.google_setup)
  /airplay/  →  127.0.0.1:8771  (jasper.web.airplay_setup)
  /sources/  →  127.0.0.1:8773  (jasper.web.sources_setup)
  /wake/     →  127.0.0.1:8774  (jasper.web.wake_setup)
  /wifi/     →  127.0.0.1:8775  (jasper.web.wifi_setup)
  /peers/    →  127.0.0.1:8776  (jasper.web.peering_setup)
  /transit/  →  127.0.0.1:8777  (jasper.web.transit_setup)
  /ha/       →  127.0.0.1:8778  (jasper.web.home_assistant_setup)
  /weather/  →  127.0.0.1:8779  (jasper.web.weather_setup)
  /speaker/  →  127.0.0.1:8783  (jasper.web.speaker_setup)
  /sound/    →  127.0.0.1:8784  (jasper.web.sound_setup)

Socket activation:
  When started by `jasper-web.socket` (systemd), the listening sockets
  for all ports are handed to us via LISTEN_FDS at process start.
  We adopt them by matching `getsockname()` port → wizard. After 10 min
  of no incoming requests on any wizard, the process exits cleanly and
  systemd's .socket goes back to listening — saving ~60-90 MB Pss when
  no one's using a setup page. Falls back to direct bind when launched
  directly (e.g. for dev/testing).

If any server fails to bind (port collision, permission), the process
exits non-zero so systemd restarts the unit. We don't try to keep some
servers alive while one is down — the user-visible symptom of a
partial start would be a 502 on the broken page, which is more
confusing than 'the whole settings host is restarting'.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from jasper import wake_ports

from . import (
    _systemd,
    airplay_setup,
    google_setup,
    home_assistant_setup,
    peering_setup,
    sound_setup,
    speaker_setup,
    sources_setup,
    spotify_setup,
    transit_setup,
    voice_setup,
    wake_setup,
    weather_setup,
    wifi_setup,
)

logger = logging.getLogger(__name__)


def _serve_forever(server, label: str) -> None:
    try:
        server.serve_forever()
    except Exception:  # noqa: BLE001
        logger.exception("jasper-web %s worker crashed", label)


def _wake_corpus_ports_from_env() -> dict[str, int]:
    """Resolve wake-corpus UDP ports for the combined jasper-web unit."""
    return wake_ports.build_ports(
        aec_on_port=int(os.environ.get(
            "JASPER_WAKE_CORPUS_AEC_ON_PORT",
            str(wake_ports.DEFAULT_AEC_ON_PORT),
        )),
        aec_off_port=int(os.environ.get(
            "JASPER_WAKE_CORPUS_AEC_OFF_PORT",
            str(wake_ports.DEFAULT_AEC_OFF_PORT),
        )),
        aec_dtln_port=int(os.environ.get(
            "JASPER_WAKE_CORPUS_AEC_DTLN_PORT",
            str(wake_ports.DEFAULT_AEC_DTLN_PORT),
        )),
        aec_raw0_port=int(os.environ.get(
            "JASPER_WAKE_CORPUS_AEC_RAW0_PORT",
            str(wake_ports.DEFAULT_AEC_RAW0_PORT),
        )),
        aec_ref_port=int(os.environ.get(
            "JASPER_WAKE_CORPUS_AEC_REF_PORT",
            str(wake_ports.DEFAULT_AEC_REF_PORT),
        )),
        aec_usb_raw_port=int(os.environ.get(
            "JASPER_WAKE_CORPUS_AEC_USB_RAW_PORT",
            str(wake_ports.DEFAULT_AEC_USB_RAW_PORT),
        )),
        aec_usb_webrtc_port=int(os.environ.get(
            "JASPER_WAKE_CORPUS_AEC_USB_WEBRTC_PORT",
            str(wake_ports.DEFAULT_AEC_USB_WEBRTC_PORT),
        )),
        aec_usb_dtln_port=int(os.environ.get(
            "JASPER_WAKE_CORPUS_AEC_USB_DTLN_PORT",
            str(wake_ports.DEFAULT_AEC_USB_DTLN_PORT),
        )),
        include_dtln=os.environ.get("JASPER_WAKE_CORPUS_DTLN", "1") != "0",
        include_usb=os.environ.get("JASPER_WAKE_CORPUS_USB", "1") != "0",
    )


def _make_lazy_wake_corpus_server(
    target,
    *,
    output_dir: Path,
    ports: dict[str, int],
    csrf_token: str,
):
    """Bind `/wake-corpus/` without importing NumPy until first use."""

    class _LazyWakeCorpusHandler(BaseHTTPRequestHandler):
        _load_lock = threading.Lock()
        _loaded = False

        @classmethod
        def _load_real_handler(cls) -> None:
            if cls._loaded:
                return
            with cls._load_lock:
                if cls._loaded:
                    return

                from . import wake_corpus_setup

                backend = wake_corpus_setup.RecordingBackend(
                    output_dir=output_dir,
                    ports=ports,
                )
                backend.start()
                real_cls = wake_corpus_setup._make_handler_class(
                    backend,
                    csrf_token,
                )

                for base in reversed(real_cls.mro()):
                    if base in {object, BaseHTTPRequestHandler}:
                        continue
                    for name, value in base.__dict__.items():
                        if name.startswith("__") or name == "log_request":
                            continue
                        setattr(cls, name, value)
                cls._loaded = True
                logger.info(
                    "jasper-web /wake-corpus loaded recorder lazily "
                    "(output=%s legs=%s)",
                    output_dir,
                    ",".join(ports.keys()),
                )

        def _delegate(self, method_name: str) -> None:
            try:
                self.__class__._load_real_handler()
            except Exception as e:  # noqa: BLE001
                logger.exception("wake-corpus lazy load failed")
                self.send_error(503, f"wake-corpus recorder unavailable: {e}")
                return
            getattr(self, method_name)()

        def do_GET(self) -> None:  # noqa: N802
            self._delegate("do_GET")

        def do_POST(self) -> None:  # noqa: N802
            self._delegate("do_POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._delegate("do_DELETE")

    return _systemd.make_http_server(target, _LazyWakeCorpusHandler)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Port assignments mirror nginx-jasper.conf and each wizard's CLI
    # default. With socket activation, we still bind these *logically*
    # via the .socket unit's ListenStream= directives; the per-port
    # match below maps fds to wizards regardless of the order systemd
    # passed them.
    spotify_port = int(os.environ.get("JASPER_SPOTIFY_WEB_PORT", "8765"))
    voice_port = int(os.environ.get("JASPER_VOICE_WEB_PORT", "8767"))
    google_port = int(os.environ.get("JASPER_GOOGLE_WEB_PORT", "8768"))
    airplay_port = int(os.environ.get("JASPER_AIRPLAY_WEB_PORT", "8771"))
    sources_port = int(os.environ.get("JASPER_SOURCES_WEB_PORT", "8773"))
    wake_port = int(os.environ.get("JASPER_WAKE_WEB_PORT", "8774"))
    wifi_port = int(os.environ.get("JASPER_WIFI_WEB_PORT", "8775"))
    peers_port = int(os.environ.get("JASPER_PEERS_WEB_PORT", "8776"))
    transit_port = int(os.environ.get("JASPER_TRANSIT_WEB_PORT", "8777"))
    ha_port = int(os.environ.get("JASPER_HA_WEB_PORT", "8778"))
    weather_port = int(os.environ.get("JASPER_WEATHER_WEB_PORT", "8779"))
    wake_corpus_port = int(os.environ.get("JASPER_WAKE_CORPUS_WEB_PORT", "8782"))
    speaker_port = int(os.environ.get("JASPER_SPEAKER_WEB_PORT", "8783"))
    sound_port = int(os.environ.get("JASPER_SOUND_WEB_PORT", "8784"))

    # Distribute systemd-passed sockets by port. Empty dict on legacy
    # direct invocation — each wizard then falls through to its own
    # (host, port) bind.
    by_port = {
        sock.getsockname()[1]: sock for sock in _systemd.adopt_systemd_sockets()
    }

    def target_for(port: int) -> object:
        return by_port.get(port, ("127.0.0.1", port))

    host_default = "127.0.0.1"  # only used for logging if no systemd fd

    # Spotify wizard
    spotify_server = spotify_setup.make_server(
        target_for(spotify_port),
        registry_path=os.environ.get(
            "JASPER_SPOTIFY_ACCOUNTS_PATH",
            spotify_setup.DEFAULT_REGISTRY_PATH,
        ),
        bounce_redirect_uri=os.environ.get("JASPER_SPOTIFY_BOUNCE_REDIRECT_URI"),
        manual_redirect_uri=os.environ.get(
            "JASPER_SPOTIFY_MANUAL_REDIRECT_URI",
            spotify_setup.DEFAULT_MANUAL_REDIRECT_URI,
        ),
        hostname=os.environ.get("JASPER_HOSTNAME", "jts.local"),
    )

    # Voice provider wizard
    voice_state = os.environ.get(
        "JASPER_VOICE_PROVIDER_FILE", voice_setup.PROVIDER_FILE,
    )
    voice_server = voice_setup.make_server(
        target_for(voice_port), state_path=voice_state,
    )

    # Google OAuth wizard
    google_registry = os.environ.get(
        "JASPER_GOOGLE_ACCOUNTS_PATH",
        "/var/lib/jasper/google/accounts.json",
    )
    google_redirect = os.environ.get(
        "GOOGLE_REDIRECT_URI", google_setup.default_redirect_uri(),
    )
    google_server = google_setup.make_server(
        target_for(google_port),
        registry_path=google_registry,
        redirect_uri=google_redirect,
    )

    # AirPlay sync-mode wizard
    airplay_state = os.environ.get(
        "JASPER_AIRPLAY_MODE_FILE", airplay_setup.MODE_FILE,
    )
    airplay_server = airplay_setup.make_server(
        target_for(airplay_port), state_path=airplay_state,
    )

    # Sources wizard — playback-source toggles, no persistent state
    # file. Shells out to systemctl for AirPlay, Spotify Connect, and
    # USB sink; DBus for BT.
    sources_server = sources_setup.make_server(target_for(sources_port))

    # Speaker display name — one user-facing name for Spotify Connect,
    # AirPlay, Bluetooth, and USB Audio.
    speaker_state = os.environ.get(
        "JASPER_SPEAKER_NAME_FILE", speaker_setup.SPEAKER_NAME_FILE,
    )
    speaker_server = speaker_setup.make_server(
        target_for(speaker_port), state_path=speaker_state,
    )

    # Wake-word page — model picker + detection layers + sensitivity.
    # Writes /var/lib/jasper/wake_model.env on model save; proxies
    # layer/sensitivity changes to jasper-control on
    # JASPER_CONTROL_BASE (default 127.0.0.1:8780).
    wake_state = os.environ.get(
        "JASPER_WAKE_MODEL_FILE", wake_setup.WAKE_MODEL_FILE,
    )
    wake_control_base = os.environ.get(
        "JASPER_CONTROL_BASE", wake_setup.DEFAULT_CONTROL_BASE,
    )
    wake_server = wake_setup.make_server(
        target_for(wake_port),
        state_path=wake_state,
        control_base=wake_control_base,
    )

    # Wi-Fi network management — scan / connect / forget. Stateless on
    # our side (NetworkManager owns the connection profile store).
    wifi_server = wifi_setup.make_server(target_for(wifi_port))

    # Multi-device peering wizard — toggle, room label, primary flag.
    # Writes /var/lib/jasper/peering.env and restarts jasper-voice +
    # jasper-control on save.
    peers_state = os.environ.get(
        "JASPER_PEERING_FILE", peering_setup.PEERING_ENV_FILE,
    )
    peers_server = peering_setup.make_server(
        target_for(peers_port), state_path=peers_state,
    )

    # Transit setup wizard — address geocode → nearest subway/bus
    # stops → save into /var/lib/jasper/transit.env. Modular over
    # jasper.transit.REGISTRY so new cities/modes plug in without
    # touching this file.
    transit_state = os.environ.get(
        "JASPER_TRANSIT_FILE", transit_setup.TRANSIT_FILE,
    )
    transit_server = transit_setup.make_server(
        target_for(transit_port), state_path=transit_state,
        weather_path=os.environ.get(
            "JASPER_WEATHER_FILE", weather_setup.WEATHER_FILE,
        ),
    )

    # Home Assistant connection wizard — mDNS discovery + LLAT paste +
    # optional conversation-agent picker. Writes
    # /var/lib/jasper/home_assistant.env (URL, token, optional agent_id)
    # and restarts jasper-voice on save. The home_assistant tool gates
    # on URL + token both being set, so a missing file leaves smart-home
    # control disabled by default.
    ha_state = os.environ.get(
        "JASPER_HA_FILE", home_assistant_setup.HA_ENV_FILE,
    )
    ha_server = home_assistant_setup.make_server(
        target_for(ha_port), state_path=ha_state,
    )

    # Weather default-location wizard — stores /var/lib/jasper/weather.env
    # with rounded coords + units for bare "what's the weather?" queries.
    weather_state = os.environ.get(
        "JASPER_WEATHER_FILE", weather_setup.WEATHER_FILE,
    )
    weather_server = weather_setup.make_server(
        target_for(weather_port),
        state_path=weather_state,
        transit_path=transit_state,
    )

    # Sound curve + preference EQ wizard. Writes the profile under
    # /var/lib/jasper and generated CamillaDSP configs under
    # /var/lib/camilladsp/configs, preserving any active room PEQs.
    sound_profile = os.environ.get(
        "JASPER_SOUND_PROFILE_PATH", sound_setup.PROFILE_PATH,
    )
    sound_config_dir = os.environ.get(
        "JASPER_SOUND_CONFIG_DIR", sound_setup.DEFAULT_CONFIG_DIR,
    )
    sound_server = sound_setup.make_server(
        target_for(sound_port),
        profile_path=sound_profile,
        config_dir=sound_config_dir,
    )

    # Wake-word corpus recorder — browser-driven recording UI for the
    # Phase 0b gold-corpus protocol. This is intentionally lazy:
    # wake_corpus_setup imports NumPy, and the combined settings host
    # should stay cheap for the common /sound, /wifi, /voice, etc.
    # paths. The first /wake-corpus request loads the recorder and
    # starts its asyncio loop for UDP capture from jasper-aec-bridge's
    # production/corpus streams.
    # Per-session CSRF token regenerated each daemon start. See
    # docs/HANDOFF-wake-training-experiment.md Phase 0b.
    wake_corpus_output = Path(
        os.environ.get(
            "JASPER_WAKE_CORPUS_OUTPUT",
            "/var/lib/jasper/enrollment_positives",
        )
    )
    wake_corpus_ports = _wake_corpus_ports_from_env()
    wake_corpus_csrf = secrets.token_hex(16)
    wake_corpus_server = _make_lazy_wake_corpus_server(
        target_for(wake_corpus_port),
        output_dir=wake_corpus_output,
        ports=wake_corpus_ports,
        csrf_token=wake_corpus_csrf,
    )

    # Idle-exit triggers when NO wizard sees a request for the window.
    # Each wizard's handler class is a `local` subclass produced inside
    # `_make_handler()` for that wizard, so they're distinct types —
    # patch each one's log_request to bump the shared tracker.
    tracker = _systemd.IdleShutdownTracker()
    for handler_cls in (
        spotify_server.RequestHandlerClass,
        voice_server.RequestHandlerClass,
        google_server.RequestHandlerClass,
        airplay_server.RequestHandlerClass,
        sources_server.RequestHandlerClass,
        speaker_server.RequestHandlerClass,
        wake_server.RequestHandlerClass,
        wifi_server.RequestHandlerClass,
        peers_server.RequestHandlerClass,
        transit_server.RequestHandlerClass,
        ha_server.RequestHandlerClass,
        weather_server.RequestHandlerClass,
        sound_server.RequestHandlerClass,
        wake_corpus_server.RequestHandlerClass,
    ):
        _systemd.install_request_idle_bump(handler_cls, tracker)
    tracker.start()

    for label, port in (
        ("/spotify", spotify_port),
        ("/voice", voice_port),
        ("/google", google_port),
        ("/airplay", airplay_port),
        ("/sources", sources_port),
        ("/speaker", speaker_port),
        ("/wake", wake_port),
        ("/wifi", wifi_port),
        ("/peers", peers_port),
        ("/transit", transit_port),
        ("/ha", ha_port),
        ("/weather", weather_port),
        ("/sound", sound_port),
        ("/wake-corpus", wake_corpus_port),
    ):
        if port in by_port:
            logger.info("jasper-web %s adopting systemd fd for port %d", label, port)
        else:
            logger.info("jasper-web %s listening on http://%s:%d", label, host_default, port)

    # Worker-thread wizards + Spotify on the main thread.
    # Spotify is the older / busier surface so we leave its
    # serve_forever on the main thread — keeps SIGTERM delivery
    # behavior the same as before (KeyboardInterrupt path returns 0).
    for label, server in (
        ("/voice", voice_server),
        ("/google", google_server),
        ("/airplay", airplay_server),
        ("/sources", sources_server),
        ("/speaker", speaker_server),
        ("/wake", wake_server),
        ("/wifi", wifi_server),
        ("/peers", peers_server),
        ("/transit", transit_server),
        ("/ha", ha_server),
        ("/weather", weather_server),
        ("/sound", sound_server),
        ("/wake-corpus", wake_corpus_server),
    ):
        threading.Thread(
            target=_serve_forever,
            args=(server, label),
            name=f"jasper-web-{label.strip('/')}",
            daemon=True,
        ).start()

    _systemd.notify_ready()
    try:
        spotify_server.serve_forever()
    except KeyboardInterrupt:
        pass
    _systemd.notify_stopping()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
