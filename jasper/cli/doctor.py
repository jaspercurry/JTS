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
    """Pull CARD=name out of an ALSA device string like 'plughw:CARD=Array'."""
    m = re.search(r"CARD=([^,\s]+)", device_str or "")
    return m.group(1) if m else None


def check_mic_card_matches_config(cfg: Config) -> CheckResult:
    """Validate the card actually configured in JASPER_MIC_DEVICE — not a
    hardcoded literal. install.sh autodetects the card name on the Pi, so
    the literal may differ from 'Array'."""
    card = _extract_card_name(cfg.mic_device)
    if card is None:
        return CheckResult(
            "mic ALSA card",
            "warn",
            f"JASPER_MIC_DEVICE='{cfg.mic_device}' has no CARD= component; "
            "skipping name check (open test will still run)",
        )
    return check_alsa_card(card, "arecord", f"mic ALSA card (CARD={card})")


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
        rec = sd.rec(
            int(0.5 * 16000), samplerate=16000, channels=1,
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
        # Just open + close — playing audio would surprise the user.
        s = sd.RawOutputStream(
            device=cfg.tts_device, samplerate=24000, channels=1, dtype="int16",
        )
        s.start()
        s.stop()
        s.close()
        return CheckResult("tts output", "ok", cfg.tts_device)
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
            "moOde REST", "warn",
            f"can't reach {cfg.moode_base_url}: {e} "
            f"(non-critical — voice transport tools won't work)",
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
            "MPD", "warn",
            f"can't connect to {cfg.mpd_host}:{cfg.mpd_port}: {e} "
            f"(non-critical for v1 — moOde may not be running)",
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
        # plug:jasper_dongle which goes dmix → hw:CARD=...), so a
        # separate literal check would just duplicate-fail.
        lambda: check_mic_card_matches_config(cfg),
        check_loopback,
        lambda: check_mic_capture(cfg),
        lambda: check_tts_open(cfg),
        lambda: check_openwakeword_model(cfg),
        lambda: check_moode_http(cfg),
        lambda: check_spotify_cache(cfg),
        lambda: check_state_dir(cfg),
        check_ram,
        lambda: check_spend_cap(cfg),
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
