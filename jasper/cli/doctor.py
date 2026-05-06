"""Preflight diagnostic CLI: `jasper-doctor`.

Codifies BRINGUP.md's smoke tests as automation. Each check returns
ok/warn/fail. Useful when something breaks at 11 PM — run this instead of
working through the runbook by hand.

Usage:
    sudo -E /opt/jasper/.venv/bin/jasper-doctor

Returns 0 if all critical checks pass, 1 otherwise.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import Config


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
RESET = "\033[0m"


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str = ""


def _run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def check_env_file() -> CheckResult:
    p = Path("/etc/jasper/jasper.env")
    if not p.exists():
        return CheckResult("env file", "fail", f"{p} missing — re-run install.sh")
    return CheckResult("env file", "ok", str(p))


def check_gemini_key(cfg: Config) -> CheckResult:
    if not cfg.gemini_api_key:
        return CheckResult("GEMINI_API_KEY", "fail", "not set in /etc/jasper/jasper.env")
    if not cfg.gemini_api_key.startswith("AIza"):
        return CheckResult(
            "GEMINI_API_KEY", "warn",
            "doesn't start with 'AIza' — may be a stale or wrong key",
        )
    return CheckResult("GEMINI_API_KEY", "ok", f"{cfg.gemini_api_key[:8]}...")


def check_alsa_card(name: str, kind: str, label: str) -> CheckResult:
    """kind is 'aplay' (playback) or 'arecord' (capture)."""
    bin_path = shutil.which(kind)
    if bin_path is None:
        return CheckResult(label, "fail", f"{kind} not in PATH")
    proc = _run([bin_path, "-L"])
    if name in proc.stdout:
        return CheckResult(label, "ok", f"CARD={name}")
    return CheckResult(
        label, "fail",
        f"no ALSA device with CARD={name} found in `{kind} -L`. "
        f"Plug in the device or fix the configured name.",
    )


def _extract_card_name(device_str: str) -> str | None:
    """Best-effort card name from JASPER_MIC_DEVICE for the arecord -L lookup.

    Accepts both legacy ALSA pcm strings (`plughw:CARD=Array`) and the
    current PortAudio-substring format (`Array`, `UMIK-2`, etc.). Returns
    None if the input is empty or an integer index — those skip the
    name-match check (the open test still runs)."""
    if not device_str or device_str.isdigit():
        return None
    m = re.search(r"CARD=([^,\s]+)", device_str)
    if m:
        return m.group(1)
    # PortAudio substring form — return as-is; check_alsa_card greps
    # arecord -L output for substring presence.
    return device_str


def check_mic_card_matches_config(cfg: Config) -> CheckResult:
    """Validate the card configured in JASPER_MIC_DEVICE is actually
    present according to `arecord -L`. install.sh autodetects on the Pi,
    so the literal may differ from 'Array'."""
    card = _extract_card_name(cfg.mic_device)
    if card is None:
        return CheckResult(
            "mic ALSA card",
            "warn",
            f"JASPER_MIC_DEVICE='{cfg.mic_device}' is empty or numeric; "
            "skipping name check (open test will still run)",
        )
    return check_alsa_card(card, "arecord", f"mic ALSA card ({card})")


def check_loopback() -> CheckResult:
    proc = _run(["aplay", "-L"])
    if "CARD=Loopback" in proc.stdout:
        return CheckResult("snd-aloop", "ok", "CARD=Loopback present")
    return CheckResult(
        "snd-aloop", "fail",
        "Loopback device missing. `sudo modprobe snd-aloop` or check "
        "/etc/modules-load.d/snd-aloop.conf",
    )


async def check_camilla_websocket(cfg: Config) -> CheckResult:
    try:
        from camilladsp import CamillaClient
        client = CamillaClient(cfg.camilla_host, cfg.camilla_port)
        await asyncio.to_thread(client.connect)
        vol = await asyncio.to_thread(client.volume.main_volume)
        return CheckResult(
            "CamillaDSP websocket", "ok",
            f"{cfg.camilla_host}:{cfg.camilla_port} volume={vol:.1f} dB",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "CamillaDSP websocket", "fail",
            f"can't reach {cfg.camilla_host}:{cfg.camilla_port}: {e}. "
            f"Check `systemctl status jasper-camilla`.",
        )


def check_mic_capture(cfg: Config) -> CheckResult:
    try:
        import numpy as np
        import sounddevice as sd
        # Open at the device's configured native rate/channels — PortAudio
        # rejects rates the device doesn't support. MicCapture downsamples
        # to 16 kHz at runtime; for the doctor's purposes we just need a
        # half-second read to confirm the device produces non-silent audio.
        rec = sd.rec(
            int(0.5 * cfg.mic_capture_rate),
            samplerate=cfg.mic_capture_rate,
            channels=cfg.mic_capture_channels,
            dtype="int16", device=cfg.mic_device, blocking=True,
        )
        peak = int(np.abs(rec).max())
        if peak == 0:
            return CheckResult(
                "mic capture", "fail",
                f"recorded silence from {cfg.mic_device} — wrong device or muted",
            )
        if peak < 100:
            return CheckResult(
                "mic capture", "warn",
                f"recording from {cfg.mic_device} but signal is very low (peak={peak})",
            )
        return CheckResult("mic capture", "ok", f"peak={peak} from {cfg.mic_device}")
    except Exception as e:  # noqa: BLE001
        return CheckResult("mic capture", "fail", f"{cfg.mic_device}: {e}")


def check_tts_open(cfg: Config) -> CheckResult:
    try:
        import sounddevice as sd
        # Open at the configured output_rate (TtsPlayout upsamples
        # Gemini's 24 kHz to that). Just open + close — playing audio
        # would surprise the user.
        s = sd.RawOutputStream(
            device=cfg.tts_device, samplerate=cfg.tts_output_rate,
            channels=1, dtype="int16",
        )
        s.start()
        s.stop()
        s.close()
        return CheckResult(
            "tts output", "ok",
            f"{cfg.tts_device} @ {cfg.tts_output_rate} Hz",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "tts output", "fail",
            f"can't open {cfg.tts_device}: {e}. "
            f"Check /root/.asoundrc and that jasper-camilla is running.",
        )


def check_openwakeword_model(cfg: Config) -> CheckResult:
    try:
        import openwakeword
        pkg_dir = Path(openwakeword.__file__).parent
        models_dir = pkg_dir / "resources" / "models"
        if not models_dir.exists():
            return CheckResult(
                "openWakeWord models", "fail",
                f"{models_dir} missing — run "
                "`/opt/jasper/.venv/bin/python -c 'import openwakeword.utils; "
                "openwakeword.utils.download_models()'`",
            )
        candidates = list(models_dir.glob(f"{cfg.wake_model}*.onnx")) + list(
            models_dir.glob(f"{cfg.wake_model}*.tflite")
        )
        if not candidates:
            return CheckResult(
                "openWakeWord models", "warn",
                f"no model file matching '{cfg.wake_model}' in {models_dir}",
            )
        return CheckResult(
            "openWakeWord models", "ok",
            f"{cfg.wake_model} → {candidates[0].name}",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult("openWakeWord models", "fail", str(e))


def check_moode_http(cfg: Config) -> CheckResult:
    # The transport tools (toggle_play_pause, skip_next, get_now_playing)
    # are always registered, so a broken moOde REST means voice commands
    # like "Hey Jasper, pause" silently fail. Treat as fail, not warn.
    try:
        import httpx
        r = httpx.get(
            f"{cfg.moode_base_url}/command/", params={"cmd": "get_volume"},
            timeout=2.0,
        )
        r.raise_for_status()
        return CheckResult("moOde REST", "ok", f"GET {cfg.moode_base_url}")
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "moOde REST", "fail",
            f"can't reach {cfg.moode_base_url}: {e}. "
            f"Music transport tools (pause, skip, now playing) won't work.",
        )


async def check_mpd(cfg: Config) -> CheckResult:
    try:
        from mpd.asyncio import MPDClient
        client = MPDClient()
        await client.connect(cfg.mpd_host, cfg.mpd_port)
        status = await client.status()
        client.disconnect()
        state = status.get("state", "?")
        return CheckResult("MPD", "ok", f"{cfg.mpd_host}:{cfg.mpd_port} state={state}")
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "MPD", "fail",
            f"can't connect to {cfg.mpd_host}:{cfg.mpd_port}: {e}. "
            f"Music transport tools depend on this; the assistant will "
            f"fail any play/pause/skip command.",
        )


def check_spotify_cache(cfg: Config) -> CheckResult:
    if not cfg.spotify_enabled:
        return CheckResult("Spotify auth", "ok", "not configured (skipped)")
    p = Path(cfg.spotify_cache_path)
    if not p.exists():
        return CheckResult(
            "Spotify auth", "warn",
            f"cache missing at {p}. Run `jasper-spotify-auth` once.",
        )
    return CheckResult("Spotify auth", "ok", f"refresh token cached at {p}")


def check_spotify_connect_device(cfg: Config) -> CheckResult:
    """Verify moOde's librespot is visible to at least one configured
    Spotify account, AND its broadcast name matches what
    JASPER_SPOTIFY_DEVICE_NAME is configured to substring-match against.

    This is the cold-start playback path: when no AirPlay is active
    (or AirPlay is connected but idle), `spotify_play` falls through
    to `resolve_target` → librespot. `_find_librespot_id` does a
    case-insensitive substring match of the configured pattern against
    `sp.devices()[].name`. If the configured pattern doesn't match
    moOde's actual broadcast name, every cold-start `play X` returns
    'no spotify target device available' — a silent severe failure
    mode this check catches.

    Reads moOde's `cfg_system.spotifyname` for the actual broadcast
    name and uses it as the source of truth for the diagnostic message
    even when nothing matches via Spotify's device list (e.g. moOde
    Spotify Connect disabled, or libreport not yet discovered)."""
    label = "Spotify Connect device"
    if not cfg.spotify_enabled:
        return CheckResult(label, "ok", "not configured (skipped)")

    moode_broadcast_name = _read_moode_spotify_name()
    moode_spotify_enabled = _read_moode_spotify_enabled()
    pattern = cfg.spotify_device_name.strip().lower()
    if not pattern:
        return CheckResult(
            label, "fail",
            "JASPER_SPOTIFY_DEVICE_NAME is empty. Set it to a substring "
            f"of moOde's broadcast name (currently {moode_broadcast_name!r}).",
        )

    # If moOde Spotify Connect is off, librespot won't broadcast.
    if moode_spotify_enabled is False:
        return CheckResult(
            label, "fail",
            "moOde's Spotify Connect renderer is disabled "
            "(cfg_system.spotifysvc=0). Enable it in moOde's web UI "
            "→ Configure → Audio → Renderers → Spotify Connect.",
        )

    # Quick sanity check: does the configured pattern even match moOde's
    # broadcast name? If not, no need to hit the Spotify API.
    if moode_broadcast_name and pattern not in moode_broadcast_name.lower():
        return CheckResult(
            label, "fail",
            f"JASPER_SPOTIFY_DEVICE_NAME={cfg.spotify_device_name!r} "
            f"is not a substring of moOde's broadcast name "
            f"{moode_broadcast_name!r}. Cold-start playback will fail "
            f"with 'no spotify target device available'. Fix in "
            f"/etc/jasper/jasper.env (e.g. set it to {moode_broadcast_name!r}).",
        )

    # Build clients and probe each account's sp.devices() for a match.
    try:
        from ..accounts import Registry
        from ..spotify_router import build_clients
        accounts = Registry.load(cfg.spotify_accounts_path)
        clients = build_clients(
            accounts,
            client_id=cfg.spotify_client_id,
            client_secret=cfg.spotify_client_secret,
            redirect_uri=cfg.spotify_redirect_uri,
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            label, "warn",
            f"could not build Spotify clients: {e}. "
            f"This usually means no accounts have OAuth tokens — visit "
            f"{cfg.spotify_setup_url} to link an account.",
        )
    if not clients:
        return CheckResult(
            label, "warn",
            f"no accounts have OAuth tokens (visit {cfg.spotify_setup_url}). "
            f"Once linked, this check will verify librespot visibility.",
        )

    matched_accounts: list[str] = []
    missed_accounts: list[str] = []
    seen_names_overall: set[str] = set()
    for account_name, ac in clients.items():
        try:
            devices = ac.sp.devices()
        except Exception as e:  # noqa: BLE001
            missed_accounts.append(f"{account_name} (devices fetch failed: {e})")
            continue
        names = [(d.get("name") or "") for d in devices.get("devices", [])]
        seen_names_overall.update(names)
        if any(pattern in n.lower() for n in names):
            matched_accounts.append(account_name)
        else:
            missed_accounts.append(account_name)

    if matched_accounts and not missed_accounts:
        return CheckResult(
            label, "ok",
            f"{cfg.spotify_device_name!r} visible to all "
            f"{len(matched_accounts)} account(s): {', '.join(matched_accounts)}",
        )
    if matched_accounts and missed_accounts:
        return CheckResult(
            label, "warn",
            f"{cfg.spotify_device_name!r} visible to {matched_accounts} "
            f"but NOT {missed_accounts}. Cold-start `play X` will work "
            f"only for the matched account(s). Try opening Spotify on the "
            f"missing account and casting to '{moode_broadcast_name}' once "
            f"to register it.",
        )
    return CheckResult(
        label, "fail",
        f"no account sees a device matching "
        f"{cfg.spotify_device_name!r}. moOde's broadcast name is "
        f"{moode_broadcast_name!r}. Devices currently visible to the "
        f"linked accounts: {sorted(seen_names_overall)}. "
        f"Fix: open Spotify on a phone/desktop logged into the linked "
        f"account, click the cast/devices icon, select "
        f"'{moode_broadcast_name}' once to make it discoverable; or "
        f"verify moOde Spotify Connect is actually broadcasting "
        f"(`avahi-browse -tr _spotify-connect._tcp`).",
    )


def _read_moode_spotify_name() -> str | None:
    """Read moOde's configured Spotify Connect broadcast name from its
    SQLite. Returns None if unreadable (off-Pi runs, etc.)."""
    return _read_moode_cfg("spotifyname")


def _read_moode_spotify_enabled() -> bool | None:
    """Returns True/False for moOde's Spotify Connect service flag, or
    None if unreadable."""
    val = _read_moode_cfg("spotifysvc")
    if val is None:
        return None
    return val == "1"


def _read_moode_cfg(param: str) -> str | None:
    """Read a single value from moOde's cfg_system table. Returns None
    if the DB is unreadable (e.g. running off the Pi)."""
    import sqlite3
    db_path = "/var/local/www/db/moode-sqlite3.db"
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT value FROM cfg_system WHERE param = ?", (param,),
            ).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def check_state_dir(cfg: Config) -> CheckResult:
    p = Path(cfg.usage_db).parent
    if not p.exists():
        return CheckResult("state dir", "warn", f"{p} missing (will be created on first run)")
    if not os.access(str(p), os.W_OK):
        return CheckResult("state dir", "fail", f"{p} not writable")
    return CheckResult("state dir", "ok", str(p))


def check_ram() -> CheckResult:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    mb = kb // 1024
                    if mb < 1500:
                        return CheckResult(
                            "RAM", "warn",
                            f"{mb} MB total — recommend 2GB Pi 5 for v1 stack",
                        )
                    return CheckResult("RAM", "ok", f"{mb} MB total")
    except Exception:  # noqa: BLE001
        pass
    return CheckResult("RAM", "warn", "couldn't read /proc/meminfo")


def check_aec_bridge_running() -> CheckResult:
    """jasper-aec-bridge runs SpeexDSP echo cancellation on the XVF
    chip's raw mic 0 (channel 2 of 6-ch firmware), with the
    moOde→camilla loopback as far-end reference. Output goes to
    LoopbackAEC which jasper-voice consumes as its mic source.

    The bridge is OPT-IN (not enabled by default) — see CLAUDE.md
    "Acoustic echo cancellation" for the rationale. So an
    "inactive + disabled" state is fine, and we differentiate it
    from "enabled but crashed"."""
    is_active = _run(["systemctl", "is-active", "jasper-aec-bridge.service"]).stdout.strip()
    is_enabled = _run(["systemctl", "is-enabled", "jasper-aec-bridge.service"]).stdout.strip()
    if is_active == "active":
        return CheckResult("AEC bridge service", "ok", "running (software AEC enabled)")
    if is_enabled in ("disabled", "static"):
        return CheckResult(
            "AEC bridge service", "ok",
            "disabled (software AEC opt-in; jasper-voice reads chip directly)",
        )
    # enabled but not active = crashed
    return CheckResult(
        "AEC bridge service", "fail",
        f"is-active='{is_active}', is-enabled='{is_enabled}'. Bridge is "
        f"enabled but not running. Check `journalctl -u jasper-aec-bridge`.",
    )


def check_aec_output_card() -> CheckResult:
    """LoopbackAEC card must be present (snd-aloop loaded with the
    second card config). Without it, the bridge can't write its
    AEC'd output and jasper-voice has no mic source."""
    proc = _run(["aplay", "-l"])
    if "LoopbackAEC" in proc.stdout:
        return CheckResult("AEC loopback card", "ok", "LoopbackAEC present")
    return CheckResult(
        "AEC loopback card", "fail",
        "card 'LoopbackAEC' missing. Verify "
        "/etc/modprobe.d/snd-aloop.conf has index=0,1 id=Loopback,LoopbackAEC.",
    )


def check_xvf_firmware_6ch() -> CheckResult:
    """6-ch firmware exposes raw mics on channels 2-5 of the XVF
    capture endpoint. The bridge depends on this — it reads channel 2."""
    p = Path("/proc/asound/Array/stream0")
    if not p.exists():
        return CheckResult("XVF firmware 6-ch", "fail", "Array card missing")
    text = p.read_text()
    if "Channels: 6" in text:
        return CheckResult("XVF firmware 6-ch", "ok", "capture is 6-channel")
    return CheckResult(
        "XVF firmware 6-ch", "warn",
        "XVF capture is not 6-channel — likely 2-ch firmware loaded. "
        "Re-flash with respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin "
        "via dfu-util to expose raw mics for the bridge.",
    )


def check_spend_cap(cfg: Config) -> CheckResult:
    try:
        from ..usage import SpendCap, UsageStore
        store = UsageStore(cfg.usage_db)
        cap = SpendCap(store, cfg.daily_spend_cap_usd)
        remaining = cap.remaining_usd()
        if not cap.allowed():
            return CheckResult(
                "daily spend cap", "warn",
                f"24h spend reached cap (${cfg.daily_spend_cap_usd:.2f}). "
                "Voice will refuse new sessions until rollover.",
            )
        return CheckResult(
            "daily spend cap", "ok",
            f"${remaining:.4f} remaining of ${cfg.daily_spend_cap_usd:.2f}",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult("daily spend cap", "warn", str(e))


def render(results: list[CheckResult]) -> int:
    print()
    print(f"{BOLD}jasper-doctor{RESET}\n")
    fails = warns = 0
    for r in results:
        if r.status == "ok":
            color, mark = GREEN, "✓"
        elif r.status == "warn":
            color, mark = YELLOW, "!"
            warns += 1
        else:
            color, mark = RED, "✗"
            fails += 1
        print(f"  {color}{mark}{RESET} {r.name:24s} {r.detail}")
    print()
    if fails:
        print(f"{RED}{fails} failed, {warns} warning(s).{RESET}")
        return 1
    if warns:
        print(f"{YELLOW}{warns} warning(s) — non-critical.{RESET}")
        return 0
    print(f"{GREEN}all checks passed.{RESET}")
    return 0


async def run_async(cfg: Config) -> list[CheckResult]:
    sync_checks: list[Callable[[], CheckResult]] = [
        check_env_file,
        lambda: check_gemini_key(cfg),
        # Derive the mic card from cfg, not a literal — install.sh
        # autodetects the actual card name on the Pi. The dongle path
        # is fully exercised by check_tts_open below (opens
        # jasper_out which fans out through dmix → hw:CARD=...), so a
        # separate literal check would just duplicate-fail.
        lambda: check_mic_card_matches_config(cfg),
        check_loopback,
        lambda: check_mic_capture(cfg),
        lambda: check_tts_open(cfg),
        lambda: check_openwakeword_model(cfg),
        lambda: check_moode_http(cfg),
        lambda: check_spotify_cache(cfg),
        lambda: check_spotify_connect_device(cfg),
        lambda: check_state_dir(cfg),
        check_ram,
        lambda: check_spend_cap(cfg),
        check_aec_bridge_running,
        check_aec_output_card,
        check_xvf_firmware_6ch,
    ]
    results = [c() for c in sync_checks]
    results.append(await check_camilla_websocket(cfg))
    results.append(await check_mpd(cfg))
    return results


def main() -> None:
    try:
        cfg = Config.from_env()
    except RuntimeError as e:
        print(f"{RED}config error: {e}{RESET}", file=sys.stderr)
        sys.exit(1)
    results = asyncio.run(run_async(cfg))
    sys.exit(render(results))


if __name__ == "__main__":
    main()
