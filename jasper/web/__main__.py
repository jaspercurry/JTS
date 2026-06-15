"""Entry point for the jasper-web systemd unit.

Starts the setup wizards for this install profile in a single process —
one ThreadingHTTPServer per nginx route. They share /var/lib/jasper as
their persistence volume and shell out to systemctl together when
something changes, so colocating them costs nothing extra and saves a
separate systemd unit per wizard. Full speakers get every wizard below;
streambox installs keep the local-renderer/DSP surfaces and omit the
voice/wake/assistant-only pages. nginx routes:

  /spotify/  →  127.0.0.1:8765  (jasper.web.spotify_setup)
  /voice/    →  127.0.0.1:8767  (jasper.web.voice_setup)
  /google/   →  127.0.0.1:8768  (jasper.web.google_setup)
  /airplay/  →  127.0.0.1:8771  (jasper.web.airplay_setup)
  /sources/  →  127.0.0.1:8773  (jasper.web.sources_setup)
  /wake/     →  127.0.0.1:8774  (jasper.web.wake_setup)
  /wifi/     →  127.0.0.1:8775  (jasper.web.wifi_setup)
  /transit/  →  127.0.0.1:8777  (jasper.web.transit_setup)
  /ha/       →  127.0.0.1:8778  (jasper.web.home_assistant_setup)
  /weather/  →  127.0.0.1:8779  (jasper.web.weather_setup)
  /wake-corpus/ → 127.0.0.1:8782  (lazy jasper.web.wake_corpus_setup)
  /speaker/  →  127.0.0.1:8783  (jasper.web.speaker_setup)
  /sound/    →  127.0.0.1:8784  (jasper.web.sound_setup)
  /rooms/    →  127.0.0.1:8785  (jasper.web.rooms_setup)

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
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import BaseRequestHandler, StreamRequestHandler
from jasper.install_profile import (
    FULL_INSTALL_PROFILE,
    STREAMBOX_INSTALL_PROFILE,
    install_role_for_profile,
    read_install_profile,
)

from . import _systemd

logger = logging.getLogger(__name__)
_FULL_ROLE = FULL_INSTALL_PROFILE
_STREAMBOX_ROLE = STREAMBOX_INSTALL_PROFILE
_FULL_ONLY = frozenset({_FULL_ROLE})
_LOCAL_AUDIO_ROLES = frozenset({_FULL_ROLE, _STREAMBOX_ROLE})


@dataclass(frozen=True)
class WizardSpec:
    """One socket-activated settings surface hosted by jasper-web."""

    label: str
    env_var: str
    default_port: int
    make_server: Callable[[object], object]
    roles: frozenset[str] = _FULL_ONLY
    main_thread: bool = False

    def port(self) -> int:
        return int(os.environ.get(self.env_var, str(self.default_port)))

    def available_for(self, role: str) -> bool:
        return role in self.roles


def _serve_forever(server, label: str) -> None:
    try:
        server.serve_forever()
    except Exception:  # noqa: BLE001
        logger.exception("jasper-web %s worker crashed", label)


def _wake_corpus_ports_from_env() -> dict[str, int]:
    """Resolve wake-corpus UDP ports for the combined jasper-web unit."""
    from jasper import wake_ports

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
        aec_chip_aec_150_port=int(os.environ.get(
            "JASPER_WAKE_CORPUS_AEC_CHIP_AEC_150_PORT",
            str(wake_ports.DEFAULT_AEC_CHIP_AEC_150_PORT),
        )),
        aec_chip_aec_210_port=int(os.environ.get(
            "JASPER_WAKE_CORPUS_AEC_CHIP_AEC_210_PORT",
            str(wake_ports.DEFAULT_AEC_CHIP_AEC_210_PORT),
        )),
        aec_xvf_raw0_webrtc_aec3_port=int(os.environ.get(
            "JASPER_WAKE_CORPUS_AEC_XVF_RAW0_WEBRTC_AEC3_PORT",
            str(wake_ports.DEFAULT_AEC_XVF_RAW0_WEBRTC_AEC3_PORT),
        )),
        aec_xvf_raw0_dtln_port=int(os.environ.get(
            "JASPER_WAKE_CORPUS_AEC_XVF_RAW0_DTLN_PORT",
            str(wake_ports.DEFAULT_AEC_XVF_RAW0_DTLN_PORT),
        )),
        aec3_sweep_ports={
            leg: int(os.environ.get(
                f"JASPER_WAKE_CORPUS_AEC3_SWEEP_{leg.upper()}_PORT",
                str(port),
            ))
            for leg, port in wake_ports.DEFAULT_AEC3_SWEEP_PORTS.items()
        },
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
                    if base in {
                        object,
                        BaseRequestHandler,
                        StreamRequestHandler,
                        BaseHTTPRequestHandler,
                    }:
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


def _make_spotify_server(target: object) -> object:
    from . import spotify_setup

    return spotify_setup.make_server(
        target,
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


def _make_voice_server(target: object) -> object:
    from . import voice_setup

    return voice_setup.make_server(
        target,
        state_path=os.environ.get(
            "JASPER_VOICE_PROVIDER_FILE",
            voice_setup.PROVIDER_FILE,
        ),
    )


def _make_google_server(target: object) -> object:
    from . import google_setup

    return google_setup.make_server(
        target,
        registry_path=os.environ.get(
            "JASPER_GOOGLE_ACCOUNTS_PATH",
            "/var/lib/jasper/google/accounts.json",
        ),
        redirect_uri=os.environ.get(
            "GOOGLE_REDIRECT_URI",
            google_setup.default_redirect_uri(),
        ),
    )


def _make_airplay_server(target: object) -> object:
    from . import airplay_setup

    return airplay_setup.make_server(
        target,
        state_path=os.environ.get(
            "JASPER_AIRPLAY_MODE_FILE",
            airplay_setup.MODE_FILE,
        ),
    )


def _make_sources_server(target: object) -> object:
    from . import sources_setup

    return sources_setup.make_server(target)


def _make_speaker_server(target: object) -> object:
    from . import speaker_setup

    return speaker_setup.make_server(
        target,
        state_path=os.environ.get(
            "JASPER_SPEAKER_NAME_FILE",
            speaker_setup.SPEAKER_NAME_FILE,
        ),
    )


def _make_wake_server(target: object) -> object:
    from . import wake_setup

    return wake_setup.make_server(
        target,
        state_path=os.environ.get(
            "JASPER_WAKE_MODEL_FILE",
            wake_setup.WAKE_MODEL_FILE,
        ),
        control_base=os.environ.get(
            "JASPER_CONTROL_BASE",
            wake_setup.DEFAULT_CONTROL_BASE,
        ),
    )


def _make_wifi_server(target: object) -> object:
    from . import wifi_setup

    return wifi_setup.make_server(target)


def _make_rooms_server(target: object) -> object:
    from . import rooms_setup

    # Speaker directory + wake-response/grouping controls. See
    # jasper/web/rooms_setup.py.
    return rooms_setup.make_server(target)


def _transit_state_path() -> str:
    from . import transit_setup

    return os.environ.get("JASPER_TRANSIT_FILE", transit_setup.TRANSIT_FILE)


def _weather_state_path() -> str:
    from . import weather_setup

    return os.environ.get("JASPER_WEATHER_FILE", weather_setup.WEATHER_FILE)


def _make_transit_server(target: object) -> object:
    from . import transit_setup

    return transit_setup.make_server(
        target,
        state_path=_transit_state_path(),
        weather_path=_weather_state_path(),
    )


def _make_ha_server(target: object) -> object:
    from . import home_assistant_setup

    return home_assistant_setup.make_server(
        target,
        state_path=os.environ.get(
            "JASPER_HA_FILE",
            home_assistant_setup.HA_ENV_FILE,
        ),
    )


def _make_weather_server(target: object) -> object:
    from . import weather_setup

    return weather_setup.make_server(
        target,
        state_path=_weather_state_path(),
        transit_path=_transit_state_path(),
    )


def _make_sound_server(target: object) -> object:
    from . import sound_setup

    return sound_setup.make_server(
        target,
        profile_path=os.environ.get(
            "JASPER_SOUND_PROFILE_PATH",
            sound_setup.PROFILE_PATH,
        ),
        config_dir=os.environ.get(
            "JASPER_SOUND_CONFIG_DIR",
            sound_setup.DEFAULT_CONFIG_DIR,
        ),
    )


def _make_wake_corpus_server(target: object) -> object:
    import secrets

    return _make_lazy_wake_corpus_server(
        target,
        output_dir=Path(
            os.environ.get(
                "JASPER_WAKE_CORPUS_OUTPUT",
                "/var/lib/jasper/enrollment_positives",
            )
        ),
        ports=_wake_corpus_ports_from_env(),
        csrf_token=secrets.token_hex(16),
    )


WIZARD_SPECS: tuple[WizardSpec, ...] = (
    WizardSpec(
        "/spotify", "JASPER_SPOTIFY_WEB_PORT", 8765,
        _make_spotify_server, roles=_LOCAL_AUDIO_ROLES, main_thread=True,
    ),
    WizardSpec("/voice", "JASPER_VOICE_WEB_PORT", 8767, _make_voice_server),
    WizardSpec("/google", "JASPER_GOOGLE_WEB_PORT", 8768, _make_google_server),
    WizardSpec(
        "/airplay", "JASPER_AIRPLAY_WEB_PORT", 8771, _make_airplay_server,
        roles=_LOCAL_AUDIO_ROLES,
    ),
    WizardSpec(
        "/sources", "JASPER_SOURCES_WEB_PORT", 8773, _make_sources_server,
        roles=_LOCAL_AUDIO_ROLES,
    ),
    WizardSpec("/wake", "JASPER_WAKE_WEB_PORT", 8774, _make_wake_server),
    WizardSpec(
        "/wifi", "JASPER_WIFI_WEB_PORT", 8775, _make_wifi_server,
        roles=_LOCAL_AUDIO_ROLES,
    ),
    WizardSpec(
        "/transit", "JASPER_TRANSIT_WEB_PORT", 8777, _make_transit_server,
    ),
    WizardSpec("/ha", "JASPER_HA_WEB_PORT", 8778, _make_ha_server),
    WizardSpec(
        "/weather", "JASPER_WEATHER_WEB_PORT", 8779, _make_weather_server,
    ),
    WizardSpec(
        "/wake-corpus", "JASPER_WAKE_CORPUS_WEB_PORT", 8782,
        _make_wake_corpus_server,
    ),
    WizardSpec(
        "/speaker", "JASPER_SPEAKER_WEB_PORT", 8783, _make_speaker_server,
        roles=_LOCAL_AUDIO_ROLES,
    ),
    WizardSpec(
        "/sound", "JASPER_SOUND_WEB_PORT", 8784, _make_sound_server,
        roles=_LOCAL_AUDIO_ROLES,
    ),
    WizardSpec(
        "/rooms", "JASPER_ROOMS_WEB_PORT", 8785, _make_rooms_server,
        roles=_LOCAL_AUDIO_ROLES,
    ),
)


def _active_install_role() -> str:
    try:
        return install_role_for_profile(read_install_profile())
    except ValueError as e:
        logger.error(
            "event=jasper_web.install_profile_invalid error=%r "
            "action=fail_closed",
            str(e),
        )
        return "invalid"


def _specs_for_role(role: str) -> tuple[WizardSpec, ...]:
    specs = tuple(spec for spec in WIZARD_SPECS if spec.available_for(role))
    if specs:
        return specs
    logger.error(
        "event=jasper_web.no_wizards_for_role role=%s action=fail_closed",
        role,
    )
    return ()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Port assignments mirror nginx-jasper.conf, jasper-web.socket, and
    # each wizard's CLI default. The registry above is the local source
    # of truth for this host: adding a wizard should add one WizardSpec,
    # one factory, and one ListenStream in deploy/jasper-web.socket.
    #
    # With socket activation, we still bind these *logically* via the
    # .socket unit's ListenStream= directives; the per-port match below
    # maps fds to wizards regardless of the order systemd passed them.
    by_port = {
        sock.getsockname()[1]: sock for sock in _systemd.adopt_systemd_sockets()
    }

    def target_for(port: int) -> object:
        return by_port.get(port, ("127.0.0.1", port))

    host_default = "127.0.0.1"  # only used for logging if no systemd fd

    role = _active_install_role()
    specs = _specs_for_role(role)
    logger.info(
        "event=jasper_web.profile role=%s wizards=%s",
        role,
        ",".join(spec.label for spec in specs),
    )

    servers: list[tuple[WizardSpec, int, object]] = []
    for spec in specs:
        port = spec.port()
        servers.append((spec, port, spec.make_server(target_for(port))))

    # Idle-exit triggers when NO wizard sees a request for the window.
    # Each wizard's handler class is a `local` subclass produced inside
    # `_make_handler()` for that wizard, so they're distinct types —
    # patch each one's log_request to bump the shared tracker.
    tracker = _systemd.IdleShutdownTracker()
    for _, _, server in servers:
        _systemd.install_request_idle_bump(server.RequestHandlerClass, tracker)
    tracker.start()

    for spec, port, _ in servers:
        if port in by_port:
            logger.info(
                "jasper-web %s adopting systemd fd for port %d",
                spec.label,
                port,
            )
        else:
            logger.info(
                "jasper-web %s listening on http://%s:%d",
                spec.label,
                host_default,
                port,
            )

    # Worker-thread wizards plus one foreground server. Full speakers keep
    # Spotify in the foreground for parity with the older topology; profiles
    # that do not host Spotify use the first role-available wizard instead.
    # Signal handling stays in the main thread either way.
    main_servers = [
        (spec, server) for spec, _, server in servers if spec.main_thread
    ]
    if len(main_servers) > 1:
        logger.error("jasper-web expected at most one main-thread wizard")
        return 1
    if main_servers:
        main_spec, main_server = main_servers[0]
    elif servers:
        main_spec, _, main_server = servers[0]
        logger.info(
            "event=jasper_web.main_thread_fallback role=%s wizard=%s",
            role,
            main_spec.label,
        )
    else:
        logger.error("jasper-web no wizards available for role=%s", role)
        return 1
    for spec, _, server in servers:
        if server is main_server:
            continue
        threading.Thread(
            target=_serve_forever,
            args=(server, spec.label),
            name=f"jasper-web-{spec.label.strip('/')}",
            daemon=True,
        ).start()

    _systemd.notify_ready()
    try:
        main_server.serve_forever()
    except KeyboardInterrupt:
        pass
    _systemd.notify_stopping()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
