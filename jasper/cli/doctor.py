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
import json
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


# ----------------------------------------------------------------------
# Per-renderer health: each daemon's own surface (HTTP / DBus / system).
# ----------------------------------------------------------------------

def check_librespot_running(cfg: Config) -> CheckResult:
    """Verify librespot is installed and the systemd unit is active.

    librespot 0.8.0 (rust) replaced go-librespot in the debian-stack
    on 2026-05-07 specifically for the configurable volume curve
    (--volume-ctrl log over 60 dB range). It has no local control
    HTTP, so health is checked via systemd state + binary version."""
    bin_path = "/usr/bin/librespot"
    if not os.path.isfile(bin_path):
        return CheckResult(
            "librespot binary", "fail",
            f"{bin_path} not present. Install: "
            "apt install raspotify (provides librespot via .deb)",
        )
    p = _run(["systemctl", "is-active", "librespot.service"])
    state = p.stdout.strip()
    if state != "active":
        return CheckResult(
            "librespot.service", "fail",
            f"systemctl is-active = '{state}'. Check: "
            "systemctl status librespot",
        )
    # Best-effort version line (librespot prints to stderr at startup)
    return CheckResult(
        "librespot.service", "ok",
        f"{bin_path} active (state file: "
        f"/run/librespot/state.json)",
    )


def check_shairport_sync_ap2() -> CheckResult:
    """Verify shairport-sync is installed with AirPlay 2 support
    AND the systemd unit is active. The Debian Trixie apt package
    is AP1-only; the migration's source-build emits a binary whose
    `-V` output contains 'AirPlay2'."""
    if shutil.which("shairport-sync") is None:
        return CheckResult(
            "shairport-sync AP2", "fail",
            "binary not found. Source-build per deploy/debian-stack/README.md",
        )
    p = _run(["shairport-sync", "-V"])
    out = (p.stdout + p.stderr).strip().split("\n")[0]
    if "AirPlay2" not in out:
        return CheckResult(
            "shairport-sync AP2", "fail",
            f"binary lacks --with-airplay-2 (got: {out!r}). "
            f"Apt's package is AP1-only; rebuild from source.",
        )
    p2 = _run(["systemctl", "is-active", "shairport-sync.service"])
    state = p2.stdout.strip()
    if state != "active":
        return CheckResult(
            "shairport-sync AP2", "fail",
            f"binary OK but systemd state={state}. "
            f"Check: journalctl -u shairport-sync",
        )
    return CheckResult("shairport-sync AP2", "ok", out)


def check_nqptp_running() -> CheckResult:
    """nqptp is required for AirPlay 2 timing. Without it,
    shairport-sync's AP2 path silently fails to handshake."""
    p = _run(["systemctl", "is-active", "nqptp.service"])
    state = p.stdout.strip()
    if state == "active":
        return CheckResult("nqptp", "ok", "active (UDP 319/320)")
    return CheckResult(
        "nqptp", "fail",
        f"state={state}. shairport-sync AP2 will not handshake "
        f"without nqptp running.",
    )


def check_apple_dongle_audio() -> CheckResult:
    """Apple's USB-C → 3.5mm Headphone Jack Adapter only exposes its
    USB Audio class interfaces when something is plugged into the
    analog 3.5mm jack. With no analog load, lsusb sees the chip but
    aplay -l shows no card "A" — and CamillaDSP fails to open the
    DAC with "Cannot get card index for A".

    This check distinguishes three states so the operator gets a
    clear signal instead of a generic ALSA error:

      - dongle absent: USB device not detected → fail
      - dongle USB-only: idVendor=05ac, idProduct=110a present but
        no `aplay -l` card with USB Audio class → warn with the
        actionable message (plug in speakers/headphones)
      - dongle audio active: card visible → ok
    """
    p = _run(["lsusb"])
    has_apple_dongle = bool(re.search(r"05ac:110a", p.stdout))
    if not has_apple_dongle:
        return CheckResult(
            "Apple dongle", "fail",
            "USB-C headphone adapter not detected (lsusb has no 05ac:110a). "
            "Plug it into the Pi.",
        )
    p = _run(["aplay", "-l"])
    has_audio_card = bool(
        re.search(r"USB-C to 3\.5mm|USB Audio.*USB Audio", p.stdout)
    )
    if has_audio_card:
        return CheckResult("Apple dongle", "ok", "USB + audio interfaces present")
    return CheckResult(
        "Apple dongle", "warn",
        "USB present but audio interfaces not enumerated. "
        "Plug speakers/headphones into the dongle's 3.5mm jack — "
        "the chip stays in low-power mode without an analog load.",
    )


def check_dongle_headphone_at_max() -> CheckResult:
    """The Apple dongle's analog Headphone control should be pinned at
    100%. Anything lower throws away analog headroom that we'd rather
    have available to the digital chain — main_volume in CamillaDSP is
    the user-facing knob, the dongle is meant to be a pass-through
    ceiling.

    `jasper-dac-init.service` sets this on every boot; if it's drifted,
    this check catches it. -36 dB at 40% was the historical "safe test"
    setting and is what triggered the audible-loudness gap that led to
    this check existing."""
    p = _run(["amixer", "-c", "A", "sget", "Headphone"])
    if p.returncode != 0:
        return CheckResult(
            "Dongle headphone gain", "fail",
            "amixer -c A sget Headphone failed — dongle not enumerated as "
            "card 'A'?",
        )
    # amixer prints "Front Left: Playback NN [PP%] [-DD.DDdB] [on]";
    # we want PP. If both channels are present, expect them equal.
    pcts = re.findall(r"\[(\d+)%\]", p.stdout)
    if not pcts:
        return CheckResult(
            "Dongle headphone gain", "warn",
            "Could not parse percent from amixer output (format change?).",
        )
    pct = int(pcts[0])
    if pct < 100:
        return CheckResult(
            "Dongle headphone gain", "warn",
            f"Headphone control at {pct}% (analog attenuation engaged). "
            "Run `sudo systemctl start jasper-dac-init` to pin at 100%.",
        )
    return CheckResult(
        "Dongle headphone gain", "ok",
        "Headphone at 100% (analog ceiling open)",
    )


def check_jasper_mux() -> CheckResult:
    """jasper-mux arbitrates which renderer plays when. Without it,
    starting Spotify while AirPlay is playing produces mixed audio
    until session_timeout expires."""
    p = _run(["systemctl", "is-active", "jasper-mux.service"])
    state = p.stdout.strip()
    if state == "active":
        return CheckResult("jasper-mux", "ok", "active (latest-source-wins)")
    return CheckResult(
        "jasper-mux", "warn",
        f"state={state}. Renderer preemption disabled — multiple "
        f"sources will play concurrently if active.",
    )


def check_bluealsa() -> CheckResult:
    """bluealsa daemon registers the A2DP profile with bluez;
    bluealsa-aplay forwards incoming A2DP audio to ALSA. Both
    must be active for "phone-as-Bluetooth-source → speaker"
    to work end-to-end."""
    p1 = _run(["systemctl", "is-active", "bluealsa.service"])
    p2 = _run(["systemctl", "is-active", "bluealsa-aplay.service"])
    s1 = p1.stdout.strip()
    s2 = p2.stdout.strip()
    if s1 == "active" and s2 == "active":
        return CheckResult("bluealsa", "ok", "daemon + aplay active")
    return CheckResult(
        "bluealsa", "fail",
        f"bluealsa={s1}, bluealsa-aplay={s2}. "
        f"Check: journalctl -u bluealsa",
    )


async def check_mpd(cfg: Config) -> CheckResult:
    """MPD is optional — only used if the operator installed it for
    local files / radio. Source-aware transport for AirPlay/Spotify/BT
    runs without MPD."""
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
            "MPD", "warn",
            f"not reachable at {cfg.mpd_host}:{cfg.mpd_port} ({e}). "
            f"MPD is optional — only needed for local files / radio.",
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
    """Verify the on-Pi librespot endpoint is visible to at least one
    configured Spotify account, with a broadcast name matching
    JASPER_SPOTIFY_DEVICE_NAME (substring match).

    This is the cold-start playback path: when no AirPlay is active,
    `spotify_play` falls through to `resolve_target` → librespot.
    `_find_librespot_id` does a case-insensitive substring match of
    the configured pattern against `sp.devices()[].name`. If the
    pattern doesn't match what librespot is broadcasting, every
    cold-start `play X` returns 'no spotify target device available'
    — a silent severe failure this check catches."""
    label = "Spotify Connect device"
    if not cfg.spotify_enabled:
        return CheckResult(label, "ok", "not configured (skipped)")

    pattern = cfg.spotify_device_name.strip().lower()
    if not pattern:
        return CheckResult(
            label, "fail",
            "JASPER_SPOTIFY_DEVICE_NAME is empty. Set it to a substring "
            "of librespot's --name (default 'JTS').",
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
            f"missing account and casting to the device once to register it.",
        )
    return CheckResult(
        label, "fail",
        f"no account sees a device matching "
        f"{cfg.spotify_device_name!r}. Devices currently visible to the "
        f"linked accounts: {sorted(seen_names_overall)}. "
        f"Fix: open Spotify on a phone/desktop logged into the linked "
        f"account, click the cast/devices icon, select the JTS speaker "
        f"once to make it discoverable; or verify librespot is running "
        f"(`systemctl status librespot`) and broadcasting "
        f"(`avahi-browse -tr _spotify-connect._tcp`).",
    )


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
    renderer→camilla loopback as far-end reference. Output goes to
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
        lambda: check_mic_card_matches_config(cfg),
        check_loopback,
        lambda: check_mic_capture(cfg),
        lambda: check_tts_open(cfg),
        lambda: check_openwakeword_model(cfg),
        # Per-renderer health: each daemon's own surface.
        lambda: check_librespot_running(cfg),
        check_shairport_sync_ap2,
        check_nqptp_running,
        check_bluealsa,
        check_jasper_mux,
        lambda: check_spotify_cache(cfg),
        lambda: check_spotify_connect_device(cfg),
        check_apple_dongle_audio,
        check_dongle_headphone_at_max,
        lambda: check_state_dir(cfg),
        check_ram,
        lambda: check_spend_cap(cfg),
        check_aec_bridge_running,
        check_aec_output_card,
        check_xvf_firmware_6ch,
        # Rotary dial: avahi advertising the control service so the
        # dial finds us via mDNS-SD, plus a heartbeat from any dial
        # currently on the network.
        check_avahi_jasper_control,
        check_dial_heartbeat,
        # Catch deployment drift on the shairport-sync.conf alsa block —
        # raw `hw:Loopback` silently breaks AirPlay (the d6c946c bug).
        check_shairport_sync_loopback_plughw,
    ]
    results = [c() for c in sync_checks]
    results.append(await check_camilla_websocket(cfg))
    # MPD is optional (only if the operator installed it for local
    # files / radio); a "not reachable" result is a warn, not a fail.
    results.append(await check_mpd(cfg))
    return results


def check_shairport_sync_loopback_plughw() -> CheckResult:
    """Verify the deployed shairport-sync.conf uses `plughw:Loopback,0,0`
    (not raw `hw:Loopback,0,0`). The Loopback substream is locked at
    48 kHz by CamillaDSP, but AirPlay is natively 44.1 kHz; raw `hw:`
    fails the rate negotiation and silently rejects every iPhone /
    Mac connection ("device shows up but won't connect"). plughw lets
    ALSA's plug layer resample on the way in.

    This caught us once when the fix in commit `d6c946c` lived on a
    feature branch and never made it to main. The check runs against
    the deployed file (not the repo) so it catches both sources of
    drift: branch that wasn't merged, and manual on-Pi edits."""
    label = "shairport-sync.conf: plughw:Loopback"
    p = Path("/etc/shairport-sync.conf")
    if not p.exists():
        return CheckResult(
            label, "warn",
            f"{p} missing — shairport-sync may not be installed.",
        )
    try:
        text = p.read_text()
    except OSError as e:
        return CheckResult(label, "warn", f"can't read {p}: {e}")
    # Look for an active (non-comment) output_device line. Comments in
    # shairport-sync.conf use //; libconfig syntax. We tolerate the
    # value being quoted or unquoted, single or double quotes.
    active_lines = [
        ln.strip() for ln in text.splitlines()
        if ln.strip().startswith("output_device")
    ]
    if not active_lines:
        return CheckResult(
            label, "warn",
            "no `output_device` directive found in alsa block; relying "
            "on shairport-sync's default (probably wrong).",
        )
    line = active_lines[0]
    if 'plughw:Loopback' in line:
        return CheckResult(
            label, "ok",
            "plughw:Loopback,0,0 (correct — ALSA plug layer resamples "
            "44.1k AirPlay → 48k Loopback)",
        )
    if '"hw:Loopback' in line or "'hw:Loopback" in line:
        return CheckResult(
            label, "fail",
            "output_device uses raw `hw:Loopback,0,0` — AirPlay sessions "
            "will be silently rejected because Loopback is locked at "
            "48 kHz and shairport requests 44.1 kHz. Symptom: iPhone / "
            "Mac sees JTS in the picker but can't establish a session. "
            "Fix: edit /etc/shairport-sync.conf, change `hw:` → `plughw:`, "
            "`systemctl restart shairport-sync`. The fix in source is "
            "deploy/debian-stack/etc/shairport-sync.conf (commit d6c946c).",
        )
    return CheckResult(
        label, "warn",
        f"output_device value not recognized: {line!r}",
    )


def check_avahi_jasper_control() -> CheckResult:
    """Verify avahi is advertising `_jasper-control._tcp` so the dial
    can find us via mDNS-SD. avahi-browse with -t (terminate after a
    few seconds) keeps this check fast even if no service is found."""
    label = "avahi: _jasper-control._tcp"
    bin_path = shutil.which("avahi-browse")
    if bin_path is None:
        return CheckResult(
            label, "warn",
            "avahi-browse missing (apt install avahi-utils) — can't "
            "verify the service is being advertised. Dial may still "
            "find us if avahi-daemon is publishing it.",
        )
    proc = _run([bin_path, "-rt", "_jasper-control._tcp"], timeout=4.0)
    if proc.returncode != 0:
        return CheckResult(
            label, "fail",
            f"avahi-browse exited {proc.returncode}. Is avahi-daemon "
            f"running? (`systemctl status avahi-daemon`).",
        )
    if "_jasper-control._tcp" not in proc.stdout:
        return CheckResult(
            label, "fail",
            "service not being advertised. Check that "
            "/etc/avahi/services/jasper-control.service exists and "
            "avahi-daemon was reloaded — re-run install.sh, or "
            "`sudo systemctl reload avahi-daemon`.",
        )
    return CheckResult(
        label, "ok",
        "advertised — dials can auto-discover via mDNS-SD",
    )


def check_dial_heartbeat() -> CheckResult:
    """Hit jasper-control's /dial/status — if a dial is on the network
    and configured to talk to us, it'll have sent at least one UDP log
    line during boot. Stale heartbeat is a warning (dial offline,
    asleep, or pointing at a different Pi)."""
    import urllib.request
    label = "dial heartbeat"
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8780/dial/status", timeout=3,
        ) as r:
            data = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            label, "warn",
            f"jasper-control /dial/status unreachable: {e}. "
            f"`systemctl status jasper-control`.",
        )
    last_seen_at = data.get("last_seen_at")
    if last_seen_at is None:
        return CheckResult(
            label, "warn",
            "no dial UDP log received since jasper-control started. "
            "Either no dial is on the network, the dial is on a "
            "different Wi-Fi, or it can't resolve us via mDNS-SD.",
        )
    age = data.get("age_seconds")
    ip = data.get("last_seen_ip")
    if age is not None and age > 300:
        return CheckResult(
            label, "warn",
            f"last dial heartbeat from {ip} was {int(age)}s ago "
            f"(>5 min). Dial may have lost Wi-Fi or been unplugged.",
        )
    return CheckResult(
        label, "ok",
        f"{ip} talking to us {int(age) if age else '<1'}s ago",
    )


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
