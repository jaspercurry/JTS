"""Preflight diagnostic CLI: `jasper-doctor`.

Codifies BRINGUP.md's smoke tests as automation. Each check returns
ok/warn/fail. Useful when something breaks at 11 PM — run this instead of
working through the runbook by hand.

Usage:
    sudo /opt/jasper/.venv/bin/jasper-doctor             # one shot
    sudo /opt/jasper/.venv/bin/jasper-doctor --watch     # loop, 5s
    sudo /opt/jasper/.venv/bin/jasper-doctor --watch -i 2  # loop, 2s

The doctor reads ``/etc/jasper/jasper.env`` and (if present)
``/var/lib/jasper/voice_provider.env`` itself — no need to source them
into the calling shell first. The wizard's voice_provider.env overrides
operator defaults, mirroring the systemd unit's ``EnvironmentFile=``
ordering. Variables already set in the calling shell win over both.

Returns 0 if all critical checks pass, 1 otherwise. --watch never
returns by itself; exits 0 on Ctrl-C.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
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


# Re-exported for back-compat with any external callers; the load
# logic now lives in jasper.env_load so other CLIs (jasper-cues)
# share the exact same env-file precedence as the doctor.
from ..env_load import ENV_FILES, load_env_files as _load_env_files
from ..env_load import parse_env_file as _parse_env_file


def check_env_file() -> CheckResult:
    p = Path("/etc/jasper/jasper.env")
    if not p.exists():
        return CheckResult("env file", "fail", f"{p} missing — re-run install.sh")
    wizard = Path("/var/lib/jasper/voice_provider.env")
    if wizard.exists():
        return CheckResult("env file", "ok", f"{p} (+ wizard {wizard.name})")
    return CheckResult("env file", "ok", str(p))


# Per-provider expected key prefix and human-readable label. The prefix
# is a soft signal — providers occasionally rotate the format, so a
# mismatch is a warn, not a fail. Source: each provider's API docs as
# of 2026-05-09.
_PROVIDER_KEY_INFO = {
    "gemini": ("GEMINI_API_KEY", "AIza", "gemini_api_key"),
    "openai": ("OPENAI_API_KEY", "sk-", "openai_api_key"),
    "grok": ("XAI_API_KEY", "xai-", "grok_api_key"),
}


def check_provider_key(cfg: Config) -> CheckResult:
    """Check that the active provider's API key is set and has the
    expected prefix. Other providers' keys are intentionally not
    checked — they may be set (so the wizard can switch without a
    re-paste) or not, and either is fine."""
    info = _PROVIDER_KEY_INFO.get(cfg.voice_provider)
    if info is None:
        return CheckResult(
            "voice provider key", "fail",
            f"unsupported JASPER_VOICE_PROVIDER={cfg.voice_provider!r}",
        )
    env_name, prefix, attr = info
    key = getattr(cfg, attr, "")
    if not key:
        return CheckResult(
            env_name, "fail",
            f"not set; required because JASPER_VOICE_PROVIDER="
            f"{cfg.voice_provider!r}. Paste at http://jts.local/voice/ "
            f"or add to /etc/jasper/jasper.env.",
        )
    if not key.startswith(prefix):
        return CheckResult(
            env_name, "warn",
            f"doesn't start with '{prefix}' — may be a stale or wrong key",
        )
    return CheckResult(env_name, "ok", f"{key[:8]}...")


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


_HW_SHORTHAND_RE = re.compile(r"^(?:plug)?hw:(\d+),(\d+)$")


def _extract_card_name(device_str: str) -> str | None:
    """Best-effort card name from JASPER_MIC_DEVICE for the arecord -L lookup.

    Accepts both legacy ALSA pcm strings (`plughw:CARD=Array`) and the
    current PortAudio-substring format (`Array`, `UMIK-2`, etc.). Returns
    None if the input is empty, an integer index, or the ``hw:N,M``
    positional shorthand — those take a different lookup path
    (`_check_arecord_l_card_device`) or skip the name-match entirely."""
    if not device_str or device_str.isdigit():
        return None
    if _HW_SHORTHAND_RE.match(device_str):
        return None
    m = re.search(r"CARD=([^,\s]+)", device_str)
    if m:
        return m.group(1)
    # PortAudio substring form — return as-is; check_alsa_card greps
    # arecord -L output for substring presence.
    return device_str


_ARECORD_L_LINE_RE = re.compile(r"^card (\d+):.*\bdevice (\d+):")


def _check_arecord_l_card_device(card: int, device: int) -> bool:
    """True if ``arecord -l`` lists card N device M.

    `arecord -L` prints PCM names like ``hw:CARD=Loopback,DEV=0`` —
    those don't include positional indices. `arecord -l` (lowercase L)
    prints the indexed form, with card and device on the same line:
        ``card 6: Loopback [Loopback], device 1: Loopback PCM ...``
    We parse that to validate the ``hw:N,M`` shorthand."""
    bin_path = shutil.which("arecord")
    if bin_path is None:
        return False
    proc = _run([bin_path, "-l"])
    for line in proc.stdout.splitlines():
        m = _ARECORD_L_LINE_RE.match(line)
        if m and int(m.group(1)) == card and int(m.group(2)) == device:
            return True
    return False


def check_mic_card_matches_config(cfg: Config) -> CheckResult:
    """Validate the card configured in JASPER_MIC_DEVICE is actually
    present. Two lookup paths depending on the format:

    - Named card (``Array``, ``CARD=UMIK-2``, ``plughw:CARD=Foo``):
      grep ``arecord -L`` for the substring.
    - Positional shorthand (``hw:7,1``, ``plughw:0,0``): parse
      ``arecord -l`` for ``card N: ... device M:``.

    install.sh autodetects on the Pi, so the literal may differ from
    'Array' — e.g. when the AEC bridge is enabled, mic moves to a
    UDP-form device (`udp:9876`) and this card check is skipped."""
    # UDP transport has no ALSA card to validate; just say so. The
    # `jasper-aec-bridge` running check covers transport liveness.
    from jasper.audio_io import parse_udp_device
    try:
        if parse_udp_device(cfg.mic_device or ""):
            return CheckResult(
                f"mic ALSA card ({cfg.mic_device})", "ok",
                "skipped — UDP transport, no ALSA card to validate",
            )
    except ValueError:
        pass  # `check_mic_capture` will report the malformed form.
    shorthand = _HW_SHORTHAND_RE.match(cfg.mic_device or "")
    if shorthand:
        card = int(shorthand.group(1))
        device = int(shorthand.group(2))
        label = f"mic ALSA card ({cfg.mic_device})"
        if _check_arecord_l_card_device(card, device):
            return CheckResult(label, "ok", f"card {card} device {device} present")
        return CheckResult(
            label, "fail",
            f"no card {card} / device {device} in `arecord -l` output. "
            f"The AEC bridge migrated to UDP in PR 2 and the old "
            f"LoopbackAEC card no longer exists — update "
            f"JASPER_MIC_DEVICE to `udp:9876` (or `Array` for chip-direct). "
            f"Verify with `aplay -l | grep Loopback` and "
            f"`systemctl status jasper-aec-bridge`.",
        )
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


def _jasper_voice_active() -> bool:
    """True if jasper-voice.service reports active. Cheap systemctl call."""
    return _run(["systemctl", "is-active", "jasper-voice.service"]).stdout.strip() == "active"


def check_mic_capture(cfg: Config) -> CheckResult:
    """Probe-open the mic device to confirm it produces non-silent audio.

    Caveat: when jasper-voice is already running, it holds the mic for
    capture and snd-aloop's exclusive-capture variants refuse a second
    opener. In that case the daemon's continued operation IS the
    evidence the device works — fall back to checking that
    jasper-voice is alive and report 'skipped' rather than spuriously
    failing.

    UDP devices (`udp:N` / `udp://HOST:N`, the AEC bridge transport
    under PR 2) aren't PortAudio devices — there's no `sd.rec` for
    them. We skip the probe entirely and let jasper-voice's continued
    operation be the evidence.
    """
    # UDP transport: no PortAudio probe possible. The bridge's
    # heartbeat (Tier 1) and `check_aec_bridge_running` already cover
    # whether the transport is alive; this check just stays out of
    # the way.
    from jasper.audio_io import parse_udp_device
    try:
        if parse_udp_device(cfg.mic_device or ""):
            return CheckResult(
                "mic capture", "ok",
                f"skipped — UDP transport ({cfg.mic_device}); "
                "see `jasper-aec-bridge` for liveness",
            )
    except ValueError as e:
        return CheckResult(
            "mic capture", "fail",
            f"malformed UDP device {cfg.mic_device!r}: {e}",
        )
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
        if _jasper_voice_active():
            return CheckResult(
                "mic capture", "ok",
                f"skipped — jasper-voice holds {cfg.mic_device} (probe error: {e})",
            )
        return CheckResult("mic capture", "fail", f"{cfg.mic_device}: {e}")


def check_tts_open(cfg: Config) -> CheckResult:
    """Verify TTS output device is enumerable. Doesn't actually open the
    stream — opening + starting a `sd.RawOutputStream` against a dmix
    device races with the running jasper-voice (which holds a writer
    open) and historically produced false-negative "can't open" errors
    while TTS was provably working. `query_devices` is enough to confirm
    the device exists in PortAudio's enumeration and has output
    channels available."""
    try:
        import sounddevice as sd
        info = sd.query_devices(cfg.tts_device)
        if not isinstance(info, dict):
            return CheckResult(
                "tts output", "fail",
                f"sd.query_devices({cfg.tts_device!r}) returned unexpected "
                f"shape {type(info).__name__}",
            )
        if int(info.get("max_output_channels", 0)) < 1:
            return CheckResult(
                "tts output", "fail",
                f"{cfg.tts_device} enumerated but reports 0 output channels. "
                f"Check /root/.asoundrc and that jasper-camilla is running.",
            )
        return CheckResult(
            "tts output", "ok",
            f"{cfg.tts_device} present (default rate "
            f"{int(info.get('default_samplerate', 0))} Hz, "
            f"out channels {info.get('max_output_channels')})",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "tts output", "fail",
            f"can't enumerate {cfg.tts_device}: {e}. "
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


def check_spotify_cache(cfg: Config) -> CheckResult:
    """Verify Spotify is authenticated. Prefers the multi-account
    registry (per-household-member accounts, the modern path) over the
    legacy single-account cache. Reports OK if either has a usable
    refresh token. The earlier "cache missing" warning was a false
    positive on installs using only the multi-account setup."""
    if not cfg.spotify_enabled:
        return CheckResult("Spotify auth", "ok", "not configured (skipped)")
    # Modern path: per-account registry at spotify_accounts_path.
    try:
        from ..accounts import Registry
        registry = Registry.load(cfg.spotify_accounts_path)
    except Exception:  # noqa: BLE001
        registry = None
    if registry is not None and registry.accounts:
        authed = []
        for acct in registry.accounts:
            try:
                if Path(acct.cache_path).exists():
                    authed.append(acct.name)
            except (OSError, AttributeError):
                pass
        if authed:
            return CheckResult(
                "Spotify auth", "ok",
                f"{len(authed)} account(s) cached: {', '.join(authed)}",
            )
        return CheckResult(
            "Spotify auth", "warn",
            f"{len(registry.accounts)} account(s) registered but no token "
            f"caches found under {Path(cfg.spotify_accounts_path).parent}/"
            f"caches/. Visit {cfg.spotify_setup_url} to re-link.",
        )
    # Fall back to legacy single-account cache for installs that
    # haven't migrated to the multi-account registry.
    p = Path(cfg.spotify_cache_path)
    if not p.exists():
        return CheckResult(
            "Spotify auth", "warn",
            f"no accounts registered ({cfg.spotify_accounts_path}) and "
            f"no legacy cache at {p}. Visit {cfg.spotify_setup_url} to "
            f"link an account.",
        )
    return CheckResult("Spotify auth", "ok", f"legacy cache at {p}")


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


def check_google_tokens(cfg: Config) -> CheckResult:
    """Verify Google OAuth state is healthy.

    Three states matter:
      - CLIENT_ID/SECRET not set → ok (skipped, not enabled)
      - CLIENT_ID/SECRET set but no accounts linked → warn (wizard
        needs visiting; Calendar/Gmail tools are silently unregistered)
      - At least one account fails to refresh → warn (likely revoked
        or password-changed; user needs to re-link)
    """
    label = "Google OAuth"
    if not cfg.google_enabled:
        return CheckResult(
            label, "ok",
            f"not configured (skipped — visit {cfg.google_setup_url} "
            f"to enable Calendar + Gmail tools)",
        )
    try:
        from ..google_creds import GoogleRegistry, valid_access_token
    except ImportError as e:
        return CheckResult(
            label, "fail",
            f"google-auth import failed: {e}. Re-run install.sh.",
        )
    registry = GoogleRegistry.load(cfg.google_accounts_path)
    if not registry.accounts:
        return CheckResult(
            label, "warn",
            f"CLIENT_ID/SECRET set but no accounts linked. Visit "
            f"{cfg.google_setup_url} to link a household member's "
            f"Calendar + Gmail.",
        )
    healthy: list[str] = []
    broken: list[str] = []
    for a in registry.accounts:
        token = valid_access_token(
            a,
            client_id=cfg.google_client_id,
            client_secret=cfg.google_client_secret,
        )
        if token:
            healthy.append(a.name)
        else:
            broken.append(a.name)
    if broken:
        return CheckResult(
            label, "warn",
            f"refresh failed for {broken}; healthy: {healthy or 'none'}. "
            f"Re-link the broken account(s) at {cfg.google_setup_url}.",
        )
    return CheckResult(
        label, "ok",
        f"{len(healthy)} account(s) refreshed: {', '.join(healthy)}",
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


def _aec_mode_setting() -> str:
    """Read JASPER_AEC_MODE from /var/lib/jasper/aec_mode.env. Returns
    'auto' (the install.sh default) when the file is missing or
    unreadable, matching the reconciler's behaviour."""
    p = Path("/var/lib/jasper/aec_mode.env")
    if not p.exists():
        return "auto"
    try:
        for line in p.read_text().split("\n"):
            line = line.strip()
            if line.startswith("JASPER_AEC_MODE="):
                return line.split("=", 1)[1].strip().strip("'\"") or "auto"
    except OSError:
        pass
    return "auto"


def check_aec_bridge_running() -> CheckResult:
    """jasper-aec-bridge runs WebRTC AEC3 echo cancellation on the XVF
    chip's ASR-tap channel (1 of the 6-ch firmware, see
    jasper/mics/xvf3800.py MIC_CHANNEL_INDEX), with the
    renderer→camilla loopback as far-end reference. Output goes over
    UDP localhost, which jasper-voice consumes as its mic source.

    AEC is the *desired* state — wake word fires more cleanly and
    false wakes during music playback drop dramatically. So we treat
    any "AEC could be on but isn't" state as a warning (gentle
    nudge), only suppressing it to ok when the operator explicitly
    opted out via JASPER_AEC_MODE=disabled. A silent-disabled bridge
    (the May 2026 reconciler bug that mis-read Playback Channels: 2
    as the capture count) shows up as a hard fail."""
    from ..mics import xvf3800
    is_active = _run(["systemctl", "is-active", "jasper-aec-bridge.service"]).stdout.strip()
    is_enabled = _run(["systemctl", "is-enabled", "jasper-aec-bridge.service"]).stdout.strip()

    if is_active == "active":
        return CheckResult("AEC bridge service", "ok", "running (software AEC enabled)")

    aec_mode = _aec_mode_setting()
    capture_ch = xvf3800.capture_channels()
    chip_present = capture_ch is not None
    is_6ch = capture_ch == xvf3800.RECOMMENDED_FIRMWARE.capture_channels

    if aec_mode != "auto":
        # Explicit operator opt-out is fine.
        return CheckResult(
            "AEC bridge service", "ok",
            f"disabled (JASPER_AEC_MODE={aec_mode})",
        )

    if not chip_present:
        return CheckResult(
            "AEC bridge service", "warn",
            f"off — {xvf3800.DISPLAY_NAME} not present. Software AEC needs it; "
            "plug it in and the reconciler will enable AEC on next event.",
        )

    if not is_6ch:
        return CheckResult(
            "AEC bridge service", "warn",
            f"off — XVF chip is on {capture_ch}-channel firmware "
            f"(need {xvf3800.RECOMMENDED_FIRMWARE.capture_channels}-ch). "
            "DFU-flash per BRINGUP.md Phase 2A.5, then: "
            "sudo systemctl start jasper-aec-reconcile",
        )

    return CheckResult(
        "AEC bridge service", "fail",
        f"is-active='{is_active}', is-enabled='{is_enabled}'. "
        f"AEC should be on (mode=auto, 6-ch firmware loaded) but bridge isn't running. "
        f"Run: sudo systemctl start jasper-aec-reconcile && "
        f"journalctl -u jasper-aec-bridge -e",
    )


# `check_aec_output_card` retired in PR 2 of the resilience-ladder
# series. The bridge previously wrote AEC'd mic to a second
# snd-aloop card (LoopbackAEC at hw:7) that jasper-voice read from;
# that card was removed because snd-aloop's kernel-side
# loopback_cable wedged on consumer SIGKILL, requiring a reboot.
# The bridge now sends over UDP localhost — no kernel-side state.
# `check_mic_capture` already verifies the new transport end-to-end
# by exercising whatever JASPER_MIC_DEVICE points at.


# Compiled once: matches the bridge's periodic RMS log lines, e.g.
# "rms over 5.0s: ref=15694 mic=2077 aec=311 → attenuation=-16.5 dB (...)".
_AEC_RMS_RE = re.compile(
    r"rms over [\d.]+s: ref=(\d+) mic=(\d+) aec=(\d+) → "
    r"attenuation=(-?\d+\.\d+) dB"
)

# Thresholds for `check_aec_bridge_output_health`.
# Ambient room (no music) puts mic at ~600 RMS at our chip-side AGC
# config; music playback puts it in the 1500-3000+ range. Threshold
# 1500 distinguishes "music playing" from "idle".
_AEC_MIC_MUSIC_THRESHOLD = 1500  # RMS
# Reference is essentially silent below this. Healthy ref during
# music is 1000+ RMS.
_AEC_REF_SILENT_THRESHOLD = 50
# Drift warning rate that flags as abnormal. The 2026-05-15 dsnoop
# rate-lock state produced ~190 drift warnings/min (~955 in 5 min);
# healthy ops have ~3 per 5 min from clock skew tolerated by the
# bridge.
_AEC_DRIFT_WARN_THRESHOLD = 30  # in 5 min


def _loopback_playback_active() -> bool:
    """True if any renderer is currently writing the music-chain loopback.

    Checked by reading `/proc/asound/Loopback/pcm0p/sub*/status`: an open
    subdevice prints `state: …\\nowner_pid: …`, a closed one prints the
    single word `closed`. The presence of any non-closed sub means a
    renderer (shairport / librespot / bluealsa) is producing right now.

    Used to gate the AEC bridge FAIL: ref-silent windows are only
    diagnostic of a broken dsnoop when music IS being routed through the
    loopback. When no renderer is writing, ref-silent is the expected
    state and mic-loud bursts come from non-loopback sources (TTS via
    jasper_out, voice in the room).
    """
    import glob
    for status_path in glob.glob("/proc/asound/Loopback/pcm0p/sub*/status"):
        try:
            with open(status_path, encoding="utf-8") as f:
                first_line = f.readline().strip()
        except OSError:
            continue
        if first_line and first_line != "closed":
            return True
    return False


def _assess_aec_bridge_output(
    journal_text: str,
    music_chain_active: bool | None = None,
) -> CheckResult:
    """Pure-function assessment of the bridge's `rms over` log
    output. Split out from `check_aec_bridge_output_health` so the
    parser can be unit-tested without mocking subprocess.

    Counts four quantities across the journal window:
      - drift_count: "drained N stale ref frames (drift)" warnings
      - silent_ref_count: windows with mic-loud (>threshold) + ref-silent
      - healthy_ref_windows: windows where ref ≥ silent-threshold (any signal)
      - healthy_windows: windows with mic-loud + meaningful attenuation

    `healthy_ref_windows` is the key signal: as long as the ref path
    delivered signal in at least ONE recent window, the dsnoop/plug
    chain demonstrably works. silent_ref windows in that case are
    explained by non-loopback acoustic sources (TTS via jasper_out,
    room voice picked up by the ASR-beam AGC) and are not a bug.

    `music_chain_active` short-circuits the FAIL for pure-voice
    sessions: when no renderer is writing the loopback, every ref
    sample is correctly silent (snd-aloop produces zeros with no
    upstream producer) so the ref-silent + mic-loud pattern proves
    nothing about the dsnoop. Pass False when a check upstream has
    verified the loopback playback side is closed; the FAIL branch
    will then return OK with an explanatory message instead. Default
    None preserves the old behavior (used by tests that want to
    exercise the journal parser in isolation).
    """
    drift_count = 0
    silent_ref_count = 0
    healthy_ref_windows = 0
    healthy_windows = 0
    total_windows = 0

    for line in journal_text.split("\n"):
        if "stale ref frames" in line and "drift" in line:
            drift_count += 1
            continue
        m = _AEC_RMS_RE.search(line)
        if not m:
            continue
        ref = int(m.group(1))
        mic = int(m.group(2))
        attn_db = float(m.group(4))
        total_windows += 1
        # ref ≥ silent-threshold = the dsnoop/plug ref chain delivered
        # real samples in this window. Any single occurrence proves the
        # chain works end-to-end.
        if ref >= _AEC_REF_SILENT_THRESHOLD:
            healthy_ref_windows += 1
        # mic > music-threshold = something acoustic was loud enough to
        # plausibly be music (ambient is ~600 RMS, well below). ref <
        # silent-threshold = ref path silent in this window.
        if mic > _AEC_MIC_MUSIC_THRESHOLD and ref < _AEC_REF_SILENT_THRESHOLD:
            silent_ref_count += 1
        # "Healthy AEC work" = music-loud mic + meaningful attenuation.
        # Below the music threshold AEC output is just noise floor so we
        # can't tell whether the attenuation number means anything.
        if mic > _AEC_MIC_MUSIC_THRESHOLD and attn_db <= -8.0:
            healthy_windows += 1

    # Failure mode 1 — ref path broken. The 2026-05-15 dsnoop rate-lock
    # signature: AirPlay was playing, mic was 2000+, ref was 0 across
    # every window for four days because the dsnoop's 48 kHz declared
    # rate mismatched shairport's locked 44.1 kHz. We only fail the
    # check when NO window has ref signal at all; otherwise the silent-
    # ref windows are mic-only artifacts (TTS via jasper_out, room
    # voice) which is the 2026-05-16 false-positive mode.
    if silent_ref_count >= 5 and healthy_ref_windows == 0:
        # Second false-positive guard: if the music chain isn't
        # currently active (no renderer writing the loopback), every
        # ref sample is correctly silent. The mic-loud bursts must be
        # from a non-loopback source (TTS via jasper_out, voice in the
        # room), so ref-silent proves nothing about the dsnoop.
        if music_chain_active is False:
            return CheckResult(
                "AEC bridge output", "ok",
                f"{silent_ref_count} mic-loud windows have "
                f"ref<{_AEC_REF_SILENT_THRESHOLD} but loopback playback is "
                f"closed (no renderer writing music) — mic-loud bursts are "
                f"TTS (jasper_out bypasses the loopback) or ambient. "
                f"Re-run doctor while music is playing to exercise the ref "
                f"path; drift={drift_count}",
            )
        return CheckResult(
            "AEC bridge output", "fail",
            f"{silent_ref_count} recent windows show mic>{_AEC_MIC_MUSIC_THRESHOLD} "
            f"RMS with ref<{_AEC_REF_SILENT_THRESHOLD} RMS and zero windows show "
            f"ref signal — bridge's reference path is delivering silence "
            f"while the mic captures audio. AEC can't cancel without a "
            f"reference. Common cause: pcm.jasper_capture dsnoop rate-locked "
            f"to a renderer's native rate that doesn't match the dsnoop's "
            f"declared slave rate. See docs/HANDOFF-aec.md § 'Lessons learned' #6.",
        )

    # Failure mode 2 — continuous drift warnings = severe clock skew
    # between ref and mic capture, or rate mismatch between the loopback
    # and the bridge's expected REF_RATE.
    if drift_count > _AEC_DRIFT_WARN_THRESHOLD:
        return CheckResult(
            "AEC bridge output", "warn",
            f"{drift_count} ref-drift warnings in last 90 s "
            f"(healthy baseline ~5 per 90 s). The ref capture is "
            f"producing samples faster than the mic capture is "
            f"consuming them — usually a rate mismatch between the "
            f"music chain loopback and the bridge's expected REF_RATE. "
            f"Check /proc/asound/Loopback/pcm0p/sub0/hw_params; "
            f"AEC effectiveness degrades when drift is severe.",
        )

    # No log windows = bridge restarted within the last 90 s OR
    # journal isn't capturing the level (unlikely on default config).
    if total_windows == 0:
        return CheckResult(
            "AEC bridge output", "ok",
            "no recent RMS windows logged "
            "(bridge may have just started)",
        )

    # silent_ref bursts with a healthy ref path = the false-positive
    # mode from 2026-05-16: TTS / wake cues / loud voice raise mic above
    # the music threshold while the loopback (correctly) carries no
    # producer audio. Surface the diagnosis so an operator who runs
    # `jasper-doctor` after seeing the old fail can confirm the path
    # is fine.
    if silent_ref_count >= 5 and healthy_ref_windows > 0:
        return CheckResult(
            "AEC bridge output", "ok",
            f"{silent_ref_count} mic-loud windows have ref<{_AEC_REF_SILENT_THRESHOLD} "
            f"(likely TTS or ambient — TTS routes through jasper_out which "
            f"bypasses the loopback by design); ref path proven healthy in "
            f"{healthy_ref_windows}/{total_windows} windows; drift={drift_count}",
        )

    # All windows quiet — speaker has been idle, nothing to assess.
    if healthy_windows == 0 and silent_ref_count == 0:
        return CheckResult(
            "AEC bridge output", "ok",
            f"no music activity in last 90 s "
            f"({total_windows} log windows; no AEC work to evaluate)",
        )

    summary = (
        f"{healthy_windows}/{total_windows} recent windows show real AEC "
        f"work (mic>{_AEC_MIC_MUSIC_THRESHOLD} + attenuation≤-8 dB); "
        f"drift={drift_count}"
    )
    if silent_ref_count:
        # Non-zero silent_ref without hitting the FAIL threshold —
        # surface as diagnostic so partial ref-path glitches are visible
        # before they tip into a sustained outage.
        summary += f"; silent-ref={silent_ref_count} (<5 = below alarm)"
    return CheckResult("AEC bridge output", "ok", summary)


def check_aec_bridge_output_health() -> CheckResult:
    """Verify the bridge isn't silently producing garbage. The bare
    `is-active` check passes whenever the process is running — but
    the bridge can be running and STILL be in a degraded state:
    1) the AEC reference path is delivering silence (the May 2026
       dsnoop rate-lock incident, which went undetected for 4 days
       because doctor only checked service liveness), or 2) the
       ref/mic clocks have drifted apart so far that the bridge
       drains stale ref frames continuously. Both modes leave the
       wake detector consuming an un-cancelled mic with music
       blasting through it, but `systemctl is-active` says ok.

    This check parses the bridge's last 90 s of `rms over` log
    lines + drift warnings and flags the two failure modes by
    pattern. 90 s is chosen to ride past the transient that
    install.sh produces during a deploy (~30-60 s where the bridge
    restarts and ref capture re-converges) without missing a
    sustained outage (the 2026-05-15 dsnoop incident lasted 4
    days). The parser logic is in `_assess_aec_bridge_output` so it
    can be exercised in unit tests without subprocess mocks."""
    is_active = _run(
        ["systemctl", "is-active", "jasper-aec-bridge.service"]
    ).stdout.strip()
    if is_active != "active":
        # Already covered by check_aec_bridge_running.
        return CheckResult(
            "AEC bridge output", "ok",
            "(bridge not running — see AEC bridge service check above)",
        )

    # Use a 90-second window, not 5 minutes. Rationale: install.sh
    # restarts the bridge during a deploy, and there's a transient
    # (~30-90 s) where the bridge is running but its ref capture
    # hasn't reconnected yet. Within 90 s of deploy completion, that
    # transient looks like the broken state we're trying to catch.
    # Looking at the most recent 90 s only avoids the false-positive
    # while still being long enough to confirm sustained failures
    # (the 2026-05-15 dsnoop incident produced ref=0 for 4 days, so
    # 90 s is more than enough to see it).
    proc = _run(
        ["journalctl", "-u", "jasper-aec-bridge.service",
         "--since", "90 sec ago", "--no-pager", "--output", "cat"],
        timeout=8.0,
    )
    if proc.returncode != 0:
        return CheckResult(
            "AEC bridge output", "warn",
            f"could not read journal: {proc.stderr.strip() or 'unknown error'}",
        )

    return _assess_aec_bridge_output(
        proc.stdout,
        music_chain_active=_loopback_playback_active(),
    )


# Threshold for `probe_aec_ref_path`. A 5 s, -26 dBFS sine through dsnoop +
# plug + the bridge's 125 Hz HPF + (default) 0 dB pre-gain lands in the low
# thousands of RMS at the bridge's `ref`. We accept anything ≥200 as proof
# the path is live — comfortably above the silent floor (a broken path
# stays at 0-50) but well below typical music-playback levels (1000+).
_PROBE_REF_PASS_THRESHOLD = 200
_PROBE_SINE_PATH = "/tmp/jasper-doctor-probe-sine.wav"
_PROBE_SINE_DURATION_S = 5.0


def probe_aec_ref_path() -> list[CheckResult]:
    """Active probe: confirm the bridge's reference path is wired
    correctly by playing a brief sine into plughw:Loopback,0,0 and
    verifying the bridge's `ref` RMS rises in the rms log over the
    test window.

    Codifies the manual differential test from 2026-05-16 (see
    docs/HANDOFF-aec.md "Lessons learned" #10). Useful when
    `check_aec_bridge_output_health` returns ok because no music has
    been playing and you want a positive confirmation that the path
    works end-to-end — or when it fails and you want to localize the
    break between the ref path, the speaker chain, and the mic.

    Refuses to run if a renderer is actively playing (would mix with
    music and disturb the operator) or if the bridge isn't active."""
    import datetime
    import math
    import struct
    import urllib.error
    import urllib.request
    import wave

    results: list[CheckResult] = []

    # Pre-flight 1 — bridge must be running. The probe inspects the
    # bridge's rms log; a stopped bridge has nothing to inspect.
    is_active = _run(
        ["systemctl", "is-active", "jasper-aec-bridge.service"]
    ).stdout.strip()
    if is_active != "active":
        results.append(CheckResult(
            "probe — bridge running", "fail",
            f"bridge state is '{is_active}'; can't probe a stopped bridge. "
            f"`systemctl status jasper-aec-bridge`.",
        ))
        return results
    results.append(CheckResult("probe — bridge running", "ok", "active"))

    # Pre-flight 2 — refuse if a renderer is currently playing. The
    # probe writes to plughw:Loopback,0,0 which is the same path the
    # renderers use; competing with active music would either get a
    # device-busy error or, worse, mix our sine into the user's music
    # for 5 s.
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8780/state", timeout=3,
        ) as r:
            state = json.loads(r.read())
        active = state.get("active_source", "idle")
        # "voice" is fine — voice TTS goes to jasper_out, not the
        # loopback. "spotify" / "airplay" would compete with us.
        if active not in ("idle", "voice"):
            results.append(CheckResult(
                "probe — renderers idle", "fail",
                f"active_source={active!r}; refuse to play test sine over "
                f"existing music. Stop {active} playback and re-run.",
            ))
            return results
        results.append(CheckResult(
            "probe — renderers idle", "ok",
            f"active_source={active!r}",
        ))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        # If jasper-control is down, we can't confirm idle. Proceed
        # anyway — aplay will refuse with EBUSY if there's a real
        # conflict on Loopback,0,0.
        results.append(CheckResult(
            "probe — renderers idle", "warn",
            f"jasper-control /state unreachable ({e}); proceeding without "
            f"idle confirmation. If a renderer is running, aplay will "
            f"refuse the device.",
        ))

    # Generate the test sine. Stereo S16_LE 48 kHz to match the dongle's
    # native rate; -26 dBFS amplitude (conversational SPL through the
    # speaker at typical main_volume).
    fs = 48000
    amp = 0.05  # -26 dBFS
    freq = 1000
    n_samples = int(_PROBE_SINE_DURATION_S * fs)
    samples = bytearray()
    for i in range(n_samples):
        v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / fs))
        samples += struct.pack("<hh", v, v)
    try:
        with wave.open(_PROBE_SINE_PATH, "wb") as f:
            f.setnchannels(2)
            f.setsampwidth(2)
            f.setframerate(fs)
            f.writeframes(samples)
    except OSError as e:
        results.append(CheckResult(
            "probe — generate sine", "fail",
            f"could not write {_PROBE_SINE_PATH}: {e}",
        ))
        return results

    # Note the journal cursor BEFORE we play, so we only assess rms
    # lines that cover the probe window. journalctl `--since` accepts
    # ISO timestamps; UTC avoids timezone surprises.
    probe_start = datetime.datetime.now(datetime.timezone.utc)
    since = probe_start.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Play the sine. plughw absorbs any rate-lock state of the loopback
    # — same reason camilla and the bridge wrap their captures in plug.
    play = _run(
        ["aplay", "-q", "-D", "plughw:Loopback,0,0", _PROBE_SINE_PATH],
        timeout=_PROBE_SINE_DURATION_S + 5.0,
    )
    try:
        os.unlink(_PROBE_SINE_PATH)
    except OSError:
        pass
    if play.returncode != 0:
        results.append(CheckResult(
            "probe — aplay sine", "fail",
            f"aplay failed: {play.stderr.strip() or f'rc={play.returncode}'}. "
            f"If 'device busy', a renderer is using Loopback,0,0; if "
            f"'invalid argument', check /proc/asound/Loopback exists.",
        ))
        return results
    results.append(CheckResult(
        "probe — aplay sine", "ok",
        f"{_PROBE_SINE_DURATION_S:.0f} s of {freq} Hz sine to plughw:Loopback,0,0",
    ))

    # Wait one bridge rms window (5 s cadence) so the post-play log
    # line is captured.
    time.sleep(6.0)

    journal = _run(
        ["journalctl", "-u", "jasper-aec-bridge.service",
         "--since", since, "--no-pager", "--output=cat"],
        timeout=5.0,
    )
    if journal.returncode != 0:
        results.append(CheckResult(
            "probe — bridge journal", "warn",
            f"could not read journal: {journal.stderr.strip()}",
        ))
        return results

    max_ref = 0
    max_mic = 0
    window_count = 0
    for line in journal.stdout.split("\n"):
        m = _AEC_RMS_RE.search(line)
        if not m:
            continue
        window_count += 1
        max_ref = max(max_ref, int(m.group(1)))
        max_mic = max(max_mic, int(m.group(2)))

    if window_count == 0:
        results.append(CheckResult(
            "probe — ref signal observed", "warn",
            "no bridge rms windows since probe start; bridge may have "
            "stalled or the journal is not capturing INFO-level lines.",
        ))
        return results

    if max_ref >= _PROBE_REF_PASS_THRESHOLD:
        results.append(CheckResult(
            "probe — ref signal observed", "ok",
            f"max ref={max_ref} across {window_count} windows "
            f"(threshold ≥{_PROBE_REF_PASS_THRESHOLD}); dsnoop/plug ref "
            f"chain healthy",
        ))
    elif max_mic >= _AEC_MIC_MUSIC_THRESHOLD:
        # Mic heard the sine, ref didn't see it — speaker chain is fine,
        # ref capture is broken. This is the PR #75 silent-ref signature
        # made trivially reproducible.
        results.append(CheckResult(
            "probe — ref signal observed", "fail",
            f"max ref={max_ref} (need ≥{_PROBE_REF_PASS_THRESHOLD}) but "
            f"max mic={max_mic} — speaker is reproducing the test tone "
            f"(mic hears it) yet ref path is silent. dsnoop/plug ref "
            f"chain is broken. See docs/HANDOFF-aec.md § 'Lessons learned' #6.",
        ))
    else:
        # Neither path saw the sine — speaker or capture is the issue,
        # not specifically the ref. Most common cause: main_volume is
        # muted, the dongle is unplugged, or the chip mic is muted.
        results.append(CheckResult(
            "probe — ref signal observed", "warn",
            f"max ref={max_ref} AND max mic={max_mic} — neither path saw "
            f"the test tone. Check that the speaker is on (main_volume "
            f"not muted), the Apple dongle is plugged in, and the chip "
            f"mic isn't muted (`jasper-doctor` mixer check).",
        ))
    return results


def _systemd_is_active(unit: str) -> bool:
    """Wrapper around `systemctl is-active`. Cheap; ~5 ms per call."""
    return _run(["systemctl", "is-active", unit]).stdout.strip() == "active"


def _module_loaded(name: str) -> bool:
    """True if `lsmod` shows the named kernel module."""
    proc = _run(["lsmod"])
    if proc.returncode != 0:
        return False
    # lsmod output: first column is the module name. Match-at-line-
    # start to avoid substring matches against unrelated modules.
    return any(
        line.split() and line.split()[0] == name
        for line in proc.stdout.splitlines()
    )


def check_usbsink_dtoverlay() -> CheckResult:
    """Verify dtoverlay=dwc2,dr_mode=peripheral is in
    /boot/firmware/config.txt. Without it, the BCM2712 OTG controller
    stays in host mode and the USB-C port is power-only; the
    jasper-usbsink wizard toggle would be greyed out and turning it
    on (manually via systemctl) would just fail at the ConfigFS UDC
    bind."""
    cfg_path = Path("/boot/firmware/config.txt")
    if not cfg_path.exists():
        return CheckResult(
            "usbsink dtoverlay", "warn",
            f"{cfg_path} missing — not running on a Pi?",
        )
    try:
        content = cfg_path.read_text()
    except OSError as e:
        return CheckResult(
            "usbsink dtoverlay", "warn",
            f"can't read {cfg_path}: {e}",
        )
    needle = "dtoverlay=dwc2,dr_mode=peripheral"
    for line in content.splitlines():
        if line.strip().startswith(needle):
            return CheckResult(
                "usbsink dtoverlay", "ok",
                "dwc2 peripheral mode enabled (USB-C is gadget-capable)",
            )
    # Not present → not a fail, the feature is opt-in. Surface as a
    # warn-with-fix so a user wondering "why is the toggle greyed
    # out?" finds the answer here. install.sh's set_usb_gadget_mode
    # is idempotent so re-running install.sh + reboot recovers.
    return CheckResult(
        "usbsink dtoverlay", "warn",
        "not set; USB sink wizard toggle will show as unavailable. "
        "Re-run scripts/deploy-to-pi.sh (or sudo install.sh) and "
        "reboot to enable.",
    )


def check_usbsink_state() -> CheckResult:
    """Status check for jasper-usbsink.service.

    When the service is active, verify the state file is being
    written recently (catches a wedged daemon that's somehow still
    showing active to systemd).

    When the service is inactive, verify libcomposite is NOT loaded —
    if it is, the previous stop didn't tear cleanly and the gadget
    descriptor is leaking RAM (~60 KB). Not catastrophic but worth
    surfacing so the operator can `sudo rmmod libcomposite` or reboot.
    """
    active = _systemd_is_active("jasper-usbsink.service")
    libcomp = _module_loaded("libcomposite")

    if not active:
        if libcomp:
            return CheckResult(
                "usbsink state", "warn",
                "service inactive but libcomposite still loaded — "
                "RAM drift from a failed stop. Reboot or "
                "`sudo rmmod u_audio libcomposite` to recover.",
            )
        return CheckResult(
            "usbsink state", "ok",
            "disabled (no RAM cost beyond ~50 KB dwc2 module)",
        )

    # Service is active. Verify the daemon is publishing state.
    state_path = Path("/run/jasper-usbsink/state.json")
    if not state_path.exists():
        return CheckResult(
            "usbsink state", "fail",
            f"service active but {state_path} missing — daemon may "
            "have crashed before publishing. Check "
            "`systemctl status jasper-usbsink` and journalctl.",
        )
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return CheckResult(
            "usbsink state", "fail",
            f"can't parse {state_path}: {e}",
        )
    updated_str = data.get("updated_at")
    if not updated_str:
        return CheckResult(
            "usbsink state", "warn",
            "state file has no updated_at field — schema drift?",
        )
    try:
        from datetime import datetime, timezone
        updated = datetime.fromisoformat(updated_str)
        age = (datetime.now(timezone.utc) - updated).total_seconds()
    except (ValueError, TypeError):
        return CheckResult(
            "usbsink state", "warn",
            f"updated_at not ISO 8601: {updated_str!r}",
        )
    # State publisher writes at 1 Hz. >10 s of staleness = wedge.
    if age > 10:
        return CheckResult(
            "usbsink state", "warn",
            f"state file {age:.0f} s stale; daemon may be wedged "
            "(systemd watchdog should catch it within 15 s — check "
            "again in a moment)",
        )
    return CheckResult(
        "usbsink state", "ok",
        f"active, playing={data.get('playing')} "
        f"host_connected={data.get('host_connected')} "
        f"rms_dbfs={data.get('rms_dbfs'):.1f}",
    )


def check_usbsink_card() -> CheckResult:
    """When jasper-usbsink is enabled, the UAC2Gadget ALSA card MUST
    be present — otherwise the init.service either didn't run or
    failed to bind to UDC."""
    if not _systemd_is_active("jasper-usbsink.service"):
        return CheckResult(
            "usbsink card", "ok",
            "service disabled — card check skipped",
        )
    if Path("/proc/asound/UAC2Gadget").is_dir():
        return CheckResult(
            "usbsink card", "ok",
            "UAC2Gadget card present (host will see JTS as USB audio)",
        )
    return CheckResult(
        "usbsink card", "fail",
        "service active but /proc/asound/UAC2Gadget missing — "
        "init.service didn't bind the UDC. Check "
        "`systemctl status jasper-usbsink-init` for the failure mode.",
    )


def check_xvf_firmware_6ch() -> CheckResult:
    """6-ch firmware exposes raw mics on channels 2-5 of the XVF
    capture endpoint. The bridge depends on this — it reads channel 2."""
    from ..mics import xvf3800
    capture_ch = xvf3800.capture_channels()
    if capture_ch is None:
        return CheckResult("XVF firmware 6-ch", "warn",
                           f"{xvf3800.ALSA_CARD_NAME} card not present")
    target = xvf3800.RECOMMENDED_FIRMWARE.capture_channels
    if capture_ch == target:
        return CheckResult("XVF firmware 6-ch", "ok",
                           f"capture is {target}-channel")
    return CheckResult(
        "XVF firmware 6-ch", "warn",
        f"capture is {capture_ch}-channel — re-flash for software AEC. "
        f"In-system DFU works while the chip is plugged in normally; "
        f"BRINGUP.md Phase 2A.5 has the full procedure. Headline: "
        f"{xvf3800.dfu_flash_command()}",
    )


def check_xvf_mixer_state() -> CheckResult:
    """The XVF chip exposes each capture channel as a kernel ALSA
    mixer slot. When the chip is flashed from 2-ch to 6-ch firmware
    mid-bringup, ALSA assigns new slots for ch2-5 with defaults of
    off / 0 dB, and `alsactl restore` persists that across reboot —
    silently killing raw mics in spite of correct chip state. The
    reconciler self-heals via xvf3800.ensure_capture_open(); this
    check flags drift if anything sets them back."""
    from ..mics import xvf3800
    if not xvf3800.is_present():
        return CheckResult("XVF mixer state", "warn",
                           f"{xvf3800.ALSA_CARD_NAME} card not present")
    # Use cget (not get) — these controls aren't part of any aggregated
    # "simple control" group, so `amixer get` misses them.
    sw = _run(["amixer", "-c", xvf3800.ALSA_CARD_NAME, "cget",
               f"name={xvf3800.MIXER_CAPTURE_SWITCH}"])
    vol = _run(["amixer", "-c", xvf3800.ALSA_CARD_NAME, "cget",
                f"name={xvf3800.MIXER_CAPTURE_VOLUME}"])
    if sw.returncode != 0 or vol.returncode != 0:
        return CheckResult("XVF mixer state", "warn", "amixer cget failed")

    def _extract_values(out: str) -> str | None:
        for line in out.split("\n"):
            if ": values=" in line:
                return line.split("values=", 1)[1].strip()
        return None

    switch = _extract_values(sw.stdout) or ""
    volume = _extract_values(vol.stdout) or ""
    switch_norm = switch.replace(" ", "")
    nch = xvf3800.RECOMMENDED_FIRMWARE.capture_channels
    expected_sw = ",".join(["on"] * nch)
    try:
        volume_vals = [int(v.strip()) for v in volume.split(",") if v.strip()]
    except ValueError:
        volume_vals = []
    volume_ok = len(volume_vals) >= nch and all(v >= 50 for v in volume_vals[:nch])

    if switch_norm == expected_sw and volume_ok:
        return CheckResult(
            "XVF mixer state", "ok",
            f"all {nch} capture channels open (switch={switch_norm}, vol={volume})",
        )

    issues = []
    if switch_norm != expected_sw:
        issues.append(f"Capture Switch is {switch_norm or '<empty>'} (expected {expected_sw})")
    if not volume_ok:
        issues.append(f"Capture Volume is {volume or '<empty>'} (expected ≥50 on all {nch})")
    return CheckResult(
        "XVF mixer state", "fail",
        " | ".join(issues)
        + ". Heal: sudo /usr/local/sbin/jasper-aec-reconcile --reason heal "
        "(reconciler will reset switch/volume + alsactl store)",
    )


def check_wifi_regdom() -> CheckResult:
    """The Pi 5's brcmfmac firmware silently suppresses off-channel
    WiFi scans when the chip's per-phy regulatory domain is unset
    (`country 99: DFS-UNSET`). The user-visible symptom is that the
    /wifi/ wizard's scan returns only the connected SSID even when
    neighbors are clearly nearby — and kernel logs fill with
    `brcmf_cfg80211_scan: Scanning suppressed: status (4)`.

    The standard documented fix is `cfg80211.ieee80211_regdom=US`
    in /boot/firmware/cmdline.txt (written by Pi Imager and by
    `raspi-config nonint do_wifi_country`). That sets cfg80211's
    *global* regdom correctly, but on the Pi 5 brcmfmac firmware
    the per-phy regdom doesn't always follow that hint. There is
    no clean fix — the chip firmware is closed-source. See the
    matching CLAUDE.md `Wi-Fi switching` section for trade-offs
    of the known workarounds."""
    proc = _run(["iw", "reg", "get"], timeout=5)
    if proc.returncode != 0:
        return CheckResult(
            "WiFi reg domain", "warn",
            "iw reg get failed; can't verify WiFi scanning is unblocked",
        )
    # `iw reg get` prints a global section followed by a per-phy section
    # for each radio. We want the phy#0 country line; that's what
    # brcmfmac actually uses to gate scans.
    phy_country: str | None = None
    in_phy = False
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if line.startswith("phy#"):
            in_phy = True
            continue
        if in_phy and line.startswith("country "):
            # "country US: DFS-FCC" → grab the US
            parts = line.split(None, 2)
            if len(parts) >= 2:
                phy_country = parts[1].rstrip(":")
            break
    if phy_country is None:
        # No phy section parseable — maybe no WiFi adapter. Soft warn,
        # not fail (Pi could legitimately be Ethernet-only).
        return CheckResult(
            "WiFi reg domain", "warn",
            "could not parse phy regdom from `iw reg get` "
            "(no WiFi adapter? — Ethernet-only Pi is fine)",
        )
    if phy_country in ("99", "00"):
        return CheckResult(
            "WiFi reg domain", "warn",
            f"phy0 regdom is '{phy_country}' (unset) — WiFi scanning "
            "is suppressed by brcmfmac; /wifi/ scan will only see "
            "the connected network. Known Pi 5 firmware bug with no "
            "clean software fix; cmdline.txt already has the standard "
            "`cfg80211.ieee80211_regdom=` hint but it doesn't always "
            "propagate to the chip's per-phy regdom. See CLAUDE.md "
            "\"Wi-Fi switching\" for workarounds (rpi-update, USB WiFi).",
        )
    return CheckResult(
        "WiFi reg domain", "ok",
        f"phy0 country={phy_country} (scans permitted)",
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


def render_json(results: list[CheckResult]) -> int:
    """Machine-readable output for the /system dashboard.

    The web UI fetches this via /system/diagnostics → jasper-control →
    sudo jasper-doctor --json. Returns the same exit-code semantics as
    text render (0 = ok or warnings only; 1 = at least one fail).

    Schema is intentionally flat — one row per check — so the
    dashboard can render a table without complex per-check logic."""
    import json as _json
    fails = sum(1 for r in results if r.status == "fail")
    warns = sum(1 for r in results if r.status == "warn")
    payload = {
        "fails": fails,
        "warns": warns,
        "results": [
            {"name": r.name, "status": r.status, "detail": r.detail}
            for r in results
        ],
    }
    print(_json.dumps(payload))
    return 1 if fails else 0


async def run_async(cfg: Config) -> list[CheckResult]:
    sync_checks: list[Callable[[], CheckResult]] = [
        check_env_file,
        lambda: check_provider_key(cfg),
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
        lambda: check_google_tokens(cfg),
        check_apple_dongle_audio,
        check_dongle_headphone_at_max,
        lambda: check_state_dir(cfg),
        check_ram,
        lambda: check_spend_cap(cfg),
        check_aec_bridge_running,
        # check_aec_output_card retired in PR 2 — see jasper.cli.doctor
        check_aec_bridge_output_health,
        check_xvf_firmware_6ch,
        check_xvf_mixer_state,
        # USB sink (jasper-usbsink) — optional fourth source. The
        # checks are RAM-aware: when the service is off they verify
        # nothing leaked, when on they verify the daemon is healthy
        # and the gadget card is registered.
        check_usbsink_dtoverlay,
        check_usbsink_state,
        check_usbsink_card,
        # WiFi: brcmfmac scan suppression is the most common
        # post-bringup foot-gun — silent except in dmesg, and
        # breaks the /wifi/ wizard's primary function.
        check_wifi_regdom,
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
    """Hit jasper-control's /dial/status. The dial firmware doesn't
    send a true periodic heartbeat — `last_seen_at` only updates when
    the user touches the dial (encoder turn, button press) or when
    the dial fires a one-shot dlog line at boot. So a connected-but-
    idle dial is indistinguishable from an offline one. We can only
    flag "never seen since the daemon started"; an old age is expected
    and not a warning."""
    import urllib.request
    label = "dial activity"
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
            "no dial seen since jasper-control started. If you don't "
            "have a dial, ignore. If you do, check that it's on Wi-Fi "
            "and resolving us via mDNS-SD.",
        )
    age = data.get("age_seconds")
    ip = data.get("last_seen_ip")
    return CheckResult(
        label, "ok",
        f"last contact from {ip} {int(age) if age else '<1'}s ago "
        f"(activity, not heartbeat — an idle dial won't show recent age)",
    )


def _watch_line(results: list[CheckResult]) -> str:
    """One-line summary for --watch mode. Counts + first non-ok name so
    a glance tells the operator whether something flipped since the last
    iteration. Timestamp on the front so the line is meaningful when
    redirected to a file."""
    fails = [r for r in results if r.status == "fail"]
    warns = [r for r in results if r.status == "warn"]
    ts = time.strftime("%H:%M:%S")
    if fails:
        first = fails[0].name
        return (
            f"{ts}  {RED}{len(fails)} fail{RESET} "
            f"{YELLOW}{len(warns)} warn{RESET}  first-fail: {first}"
        )
    if warns:
        first = warns[0].name
        return (
            f"{ts}  {GREEN}ok{RESET} "
            f"{YELLOW}{len(warns)} warn{RESET}  first-warn: {first}"
        )
    return f"{ts}  {GREEN}all {len(results)} checks ok{RESET}"


async def _watch_loop(cfg: Config, interval: float) -> int:
    """Run checks every `interval` seconds, print one line per pass.
    Returns 0 on Ctrl-C."""
    print(
        f"jasper-doctor --watch (interval={interval:.1f}s, "
        f"Ctrl-C to exit)\n",
        flush=True,
    )
    try:
        while True:
            results = await run_async(cfg)
            print(_watch_line(results), flush=True)
            await asyncio.sleep(interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nexiting", flush=True)
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jasper-doctor",
        description="JTS preflight diagnostics. Run as root.",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Loop the checks until Ctrl-C; one summary line per pass.",
    )
    parser.add_argument(
        "-i", "--interval", type=float, default=5.0,
        help="Seconds between iterations in --watch mode (default 5).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON on stdout instead of the ANSI report. Used by "
             "the /system dashboard's diagnostics disclosure.",
    )
    parser.add_argument(
        "--probe-aec", action="store_true",
        help="Active probe — play a brief sine into plughw:Loopback,0,0 "
             "and verify the AEC bridge's `ref` rises in its rms log. "
             "Skips the standard checks and runs only this one test. "
             "Refuses if a renderer is currently playing.",
    )
    args = parser.parse_args()
    _load_env_files()
    try:
        cfg = Config.from_env()
    except RuntimeError as e:
        if args.json:
            import json as _json
            print(_json.dumps({
                "error": f"config: {e}", "fails": 1, "warns": 0, "results": [],
            }))
            sys.exit(1)
        print(f"{RED}config error: {e}{RESET}", file=sys.stderr)
        sys.exit(1)
    if args.probe_aec:
        results = probe_aec_ref_path()
        if args.json:
            sys.exit(render_json(results))
        sys.exit(render(results))
    if args.watch:
        sys.exit(asyncio.run(_watch_loop(cfg, args.interval)))
    results = asyncio.run(run_async(cfg))
    if args.json:
        sys.exit(render_json(results))
    sys.exit(render(results))


if __name__ == "__main__":
    main()
