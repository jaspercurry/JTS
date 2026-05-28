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
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..camilla_config_contract import DEFAULT_VOLUME_LIMIT_DB
from ..config import Config
from ..env_load import load_env_files as _load_env_files
from ..env_load import parse_env_file as _shared_parse_env_file


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


DoctorCheck = Callable[[], CheckResult] | tuple[str, Callable[[], CheckResult]]
_EXCEPTION_DETAIL_LIMIT = 240
_BEARER_SECRET_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b"
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"password|psk|token)"
    r"\s*([=:])\s*(['\"]?)([^'\"\s,;]+)"
)
_SECRET_PREFIX_RE = re.compile(r"\b(?:AIza|sk-|xai-)[A-Za-z0-9_-]{8,}")


def _redact_exception_message(message: str) -> str:
    message = _BEARER_SECRET_RE.sub("Bearer <redacted>", message)
    message = _KEY_VALUE_SECRET_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}<redacted>",
        message,
    )
    return _SECRET_PREFIX_RE.sub(
        lambda m: f"{m.group(0)[:4]}...{m.group(0)[-4:]}",
        message,
    )


def _exception_detail(exc: BaseException) -> str:
    message = _redact_exception_message(str(exc))
    if len(message) > _EXCEPTION_DETAIL_LIMIT:
        message = message[: _EXCEPTION_DETAIL_LIMIT - 3] + "..."
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message}"


def _crashed_check_result(name: str, exc: BaseException) -> CheckResult:
    return CheckResult(
        name,
        "fail",
        f"check crashed: {_exception_detail(exc)}",
    )


def _check_name(check: Callable[[], CheckResult]) -> str:
    name = getattr(check, "__name__", "doctor check")
    if name == "<lambda>":
        return "doctor check"
    if name.startswith("check_"):
        name = name[len("check_"):]
    return name.replace("_", " ")


def _normalize_doctor_check(
    entry: DoctorCheck,
) -> tuple[str, Callable[[], CheckResult]]:
    if isinstance(entry, tuple):
        return entry
    return _check_name(entry), entry


def _run_doctor_check(entry: DoctorCheck) -> CheckResult:
    name, check = _normalize_doctor_check(entry)
    try:
        return check()
    except Exception as e:  # noqa: BLE001
        return _crashed_check_result(name, e)


async def _run_async_doctor_check(
    name: str,
    check: Callable[[], Awaitable[CheckResult]],
) -> CheckResult:
    try:
        return await check()
    except Exception as e:  # noqa: BLE001
        return _crashed_check_result(name, e)


def _run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _parse_env_file(path: str) -> dict[str, str]:
    """Back-compat wrapper for tests and external doctor consumers."""

    return _shared_parse_env_file(path)


def check_env_file() -> CheckResult:
    p = Path("/etc/jasper/jasper.env")
    if not p.exists():
        return CheckResult("env file", "fail", f"{p} missing — re-run install.sh")
    wizard = Path("/var/lib/jasper/voice_provider.env")
    if wizard.exists():
        return CheckResult("env file", "ok", f"{p} (+ wizard {wizard.name})")
    return CheckResult("env file", "ok", str(p))


def check_speaker_name() -> CheckResult:
    from ..speaker_name import STATE_FILE, read_state

    state = read_state()
    p = Path(STATE_FILE)
    if p.exists() and state.source != "state":
        return CheckResult(
            "speaker name",
            "warn",
            f"{p} exists but could not be parsed; using {state.name!r}",
        )
    return CheckResult(
        "speaker name",
        "ok",
        f"{state.name!r} ({state.source})",
    )


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
        try:
            clipped = await asyncio.to_thread(client.status.clipped_samples)
            clipped_msg = f" clipped_samples={clipped}"
        except Exception:  # noqa: BLE001
            clipped_msg = " clipped_samples=?"
        if float(vol) > DEFAULT_VOLUME_LIMIT_DB + 0.1:
            return CheckResult(
                "CamillaDSP websocket", "fail",
                f"{cfg.camilla_host}:{cfg.camilla_port} volume={vol:.1f} dB "
                f"above {DEFAULT_VOLUME_LIMIT_DB:.1f} dB safety ceiling."
                f"{clipped_msg}",
            )
        return CheckResult(
            "CamillaDSP websocket", "ok",
            f"{cfg.camilla_host}:{cfg.camilla_port} volume={vol:.1f} dB"
            f"{clipped_msg}",
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
    if cfg.tts_transport == "outputd":
        socket_path = cfg.tts_outputd_socket
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(socket_path)
            return CheckResult(
                "tts output",
                "ok",
                f"outputd transport reachable at {socket_path}",
            )
        except OSError as e:
            return CheckResult(
                "tts output",
                "fail",
                f"JASPER_TTS_TRANSPORT=outputd but {socket_path} is not reachable: {e}. "
                "Start jasper-outputd or deploy main to return to the "
                "sounddevice path.",
            )
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
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
                f"Check /etc/asound.conf and that jasper-camilla is running.",
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
            f"Check /etc/asound.conf and that jasper-camilla is running.",
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
    source selection and guarded handoff stop working; if fan-in has
    restarted into its safe NONE state, music may stay silent."""
    p = _run(["systemctl", "is-active", "jasper-mux.service"])
    state = p.stdout.strip()
    if state == "active":
        return CheckResult(
            "jasper-mux", "ok",
            "active (source selection + latest-source-wins)",
        )
    return CheckResult(
        "jasper-mux", "fail",
        f"state={state}. Source selection and guarded handoff are "
        f"unavailable; fan-in may remain silent until mux is restarted.",
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
    configured Spotify account, with a broadcast name matching the
    /speaker/ display name (substring match).

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
            "speaker name is empty. Visit http://jts.local/speaker/ "
            "and set a display name (default 'JTS').",
        )

    # Build clients and probe each account's sp.devices() for a match.
    try:
        from ..accounts import Registry
        from ..spotify_router import build_clients
        accounts = Registry.load(cfg.spotify_accounts_path)
        result = build_clients(
            accounts,
            client_id=cfg.spotify_client_id,
            redirect_uri=cfg.spotify_redirect_uri,
        )
        clients = result.clients
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
        f"account, click the cast/devices icon, select the speaker "
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


def check_home_assistant(cfg: Config) -> CheckResult:
    """Verify Home Assistant connectivity for the home_assistant voice tool.

    Three states matter:
      - URL or token not set → ok (skipped, not enabled). The home_assistant
        tool is gated on both being present.
      - Both set, but GET /api/ fails (network, auth, 5xx) → fail with an
        actionable hint pointing at the setup wizard.
      - Both set, GET /api/ succeeds → ok with the instance name + version.

    Mirrors the skip-if-not-configured pattern of check_google_tokens.
    Synchronous wrapper around the async probe so it slots into run_async's
    sync-check list without restructuring.
    """
    import asyncio as _asyncio

    label = "Home Assistant"
    setup_url = f"http://{cfg.hostname}/ha"
    if not cfg.ha_enabled:
        return CheckResult(
            label, "ok",
            f"not configured (skipped — visit {setup_url} to enable "
            f"smart-home control)",
        )
    try:
        from ..home_assistant import probe_status
    except ImportError as e:
        return CheckResult(label, "fail", f"home_assistant import failed: {e}")
    try:
        # force=True bypasses probe_status's 15s cache — the doctor is
        # an ad-hoc diagnostic, not a polling consumer, and the user
        # running `jasper-doctor` expects fresh ground truth.
        result = _asyncio.run(probe_status(
            cfg.ha_url, cfg.ha_token,
            force=True,
            verify_ssl=bool(getattr(cfg, "ha_verify_ssl", True)),
        ))
    except Exception as e:  # noqa: BLE001
        return CheckResult(label, "fail", f"probe raised: {e}")
    if not result.get("connected"):
        return CheckResult(
            label, "fail",
            f"configured but unreachable at {result.get('url') or cfg.ha_url}: "
            f"{result.get('error') or 'unknown error'}. Re-check the URL "
            f"and token at {setup_url}.",
        )
    name = result.get("instance_name") or "Home Assistant"
    version = result.get("version") or "?"
    return CheckResult(
        label, "ok",
        f"connected to {name} ({version}) at {result.get('url')}",
    )


def check_citibike(cfg: Config) -> CheckResult:
    """Verify Citi Bike GBFS reachability + saved-station resolution.

    Four states (mirrors `check_home_assistant`'s skip-if-not-
    configured pattern):
      - No saved stations → ok (skipped). Tool isn't registered.
      - Saved stations, GBFS unreachable → fail. Tool will degrade to
        cached / error responses at runtime.
      - Saved stations, GBFS responsive, all saved IDs present in
        the current station_information.json → ok with the count
        (and an "(e-bike-only mode)" suffix when the global flag is
        set).
      - Saved stations, GBFS responsive, one or more saved IDs
        missing → warn with the affected labels. Lyft periodically
        retires stations; the user has to re-pick at /transit/.
    """
    label = "Citi Bike"
    setup_url = f"http://{cfg.hostname}/transit"
    if not cfg.citibike_enabled:
        return CheckResult(
            label, "ok",
            f"not configured (skipped — visit {setup_url} to enable)",
        )
    try:
        from ..citibike import (
            INFO_TTL_SECONDS,
            STATION_INFO_URL,
            fetch_feed,
        )
    except ImportError as e:
        return CheckResult(label, "fail", f"citibike module import failed: {e}")
    try:
        info = fetch_feed(STATION_INFO_URL, INFO_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            label, "fail",
            f"GBFS unreachable: {e}. Saved-station drift cannot be "
            f"validated; voice tool will degrade to cached data or "
            f"return {{error}} at runtime.",
        )
    known_ids = {
        s.get("station_id")
        for s in (info.get("data") or {}).get("stations", [])
        if isinstance(s, dict)
    }
    saved = list(cfg.citibike_stations)
    missing = [(sid, lab) for sid, lab in saved if sid not in known_ids]
    if missing:
        names = ", ".join(lab for _, lab in missing[:3])
        suffix = "" if len(missing) <= 3 else f" (+{len(missing) - 3} more)"
        return CheckResult(
            label, "warn",
            f"{len(missing)}/{len(saved)} saved station(s) no longer in "
            f"GBFS — Lyft retired them: {names}{suffix}. "
            f"Re-pick at {setup_url}.",
        )
    extra = " (e-bike-only mode)" if cfg.citibike_ebike_only else ""
    return CheckResult(
        label, "ok",
        f"connected — {len(saved)} saved station"
        f"{'s' if len(saved) != 1 else ''}{extra}",
    )


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


def _meminfo_kb(field: str) -> int | None:
    """Read a single field (e.g. 'MemAvailable') from /proc/meminfo
    in KiB. Returns None on read error."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except Exception:  # noqa: BLE001
        return None
    return None


def check_memory_headroom() -> CheckResult:
    """Live memory pressure check: WARN if MemAvailable is so low that
    the next ad-hoc allocation will tip the box into zram-thrash.

    Thresholds are percentage-of-RAM with absolute MB floors, so this
    fires sanely on every Pi SKU (1 GB through 16 GB) without needing
    per-tier branching:
      warn if  available < max(100 MB, 10% of total)
      fail if  available < max(30 MB,  3% of total)

    On 1 GB:  warn at 100 MB, fail at 30 MB
    On 2 GB:  warn at 200 MB, fail at 60 MB
    On 8 GB:  warn at 800 MB, fail at 240 MB

    The 2026-05-23 incident shape was MemAvailable falling from
    ~250 MB to single-digit MB over ~10 s as a PIO compile ramped
    up; this check catches that BEFORE the wedge if the operator
    runs the doctor first."""
    total_kb = _meminfo_kb("MemTotal") or 0
    avail_kb = _meminfo_kb("MemAvailable")
    if avail_kb is None or total_kb == 0:
        return CheckResult(
            "memory headroom", "warn", "couldn't read /proc/meminfo",
        )
    avail_mb = avail_kb // 1024
    total_mb = total_kb // 1024
    pct = (avail_kb * 100) // total_kb if total_kb else 0
    # Percentage-with-floor pattern — see Prometheus node_exporter
    # alert conventions and Pop!_OS pop-os/default-settings#163.
    fail_mb = max(30, total_mb * 3 // 100)
    warn_mb = max(100, total_mb * 10 // 100)
    if avail_mb < fail_mb:
        return CheckResult(
            "memory headroom", "fail",
            f"only {avail_mb} MB available ({pct}%) — OOM imminent "
            f"(fail threshold {fail_mb} MB)",
        )
    if avail_mb < warn_mb:
        return CheckResult(
            "memory headroom", "warn",
            f"only {avail_mb} MB available ({pct}%) — tight "
            f"(warn threshold {warn_mb} MB)",
        )
    return CheckResult(
        "memory headroom", "ok",
        f"{avail_mb} MB available ({pct}%)",
    )


def check_zram_size_ratio() -> CheckResult:
    """Verify the rpi-swap drop-in sized zram to ≤60% of RAM. The
    old zramswap default was 100% of RAM, which amplifies thrash
    (more zsmalloc bookkeeping during reclaim). Stage 1 of the
    memory-resilience plan reduces this to 50%.

    Skip cleanly if:
      - zram isn't in use at all (older RPi OS / dphys-swapfile setups)
      - rpi-swap isn't installed (Bookworm or earlier — JTS's drop-in
        targets rpi-swap exclusively, so on other zram managers there
        is no actionable fix for the operator from this side)"""
    try:
        zram_size_bytes = int(Path("/sys/block/zram0/disksize").read_text().strip())
    except (OSError, ValueError):
        return CheckResult(
            "zram size", "ok", "no zram0 device (rpi-swap not active)",
        )
    if zram_size_bytes == 0:
        return CheckResult("zram size", "ok", "zram0 present but unsized")
    total_kb = _meminfo_kb("MemTotal") or 0
    if total_kb == 0:
        return CheckResult("zram size", "warn", "couldn't compute ratio")
    total_bytes = total_kb * 1024
    pct = (zram_size_bytes * 100) // total_bytes
    zram_mb = zram_size_bytes // (1024 * 1024)
    if pct > 60:
        # If rpi-swap isn't installed, the JTS drop-in is moot —
        # different package owns the zram device. Don't warn the
        # operator about something they can't fix from this side.
        # Detection: /etc/rpi/swap.conf exists iff rpi-swap is the
        # canonical Pi-side zram manager (Trixie default).
        if not Path("/etc/rpi/swap.conf").exists():
            return CheckResult(
                "zram size", "ok",
                f"{zram_mb} MB ({pct}% of RAM) — managed by a different "
                f"zram package (rpi-swap not installed); JTS drop-in is inert",
            )
        return CheckResult(
            "zram size", "warn",
            f"{zram_mb} MB ({pct}% of RAM) — old default; "
            f"Stage 1 plan recommends 50%. If the drop-in is present "
            f"(check /etc/rpi/swap.conf.d/50-jts.conf), reboot to apply "
            f"— rpi-swap is a generator (runs at boot, not a service).",
        )
    return CheckResult(
        "zram size", "ok", f"{zram_mb} MB ({pct}% of RAM)",
    )


def check_mglru_min_ttl() -> CheckResult:
    """Verify MGLRU min_ttl_ms is set to prevent thrashing under
    memory pressure. Stage 1 of the memory-resilience plan ships
    1000 ms via /etc/tmpfiles.d/jts-mglru.conf. Skip cleanly on
    kernels without MGLRU (< 6.1) — the tmpfiles config uses
    `w-` which silently no-ops there."""
    p = Path("/sys/kernel/mm/lru_gen/min_ttl_ms")
    if not p.exists():
        return CheckResult(
            "MGLRU min_ttl", "ok",
            "kernel lacks MGLRU (< 6.1) — thrash prevention via watermarks only",
        )
    try:
        v = int(p.read_text().strip())
    except (OSError, ValueError):
        return CheckResult("MGLRU min_ttl", "warn", "couldn't read value")
    if v == 0:
        return CheckResult(
            "MGLRU min_ttl", "warn",
            "0 ms (default) — thrash prevention disabled. "
            "Run `sudo systemd-tmpfiles --create /etc/tmpfiles.d/jts-mglru.conf` "
            "or re-run install.sh.",
        )
    if v != 1000:
        return CheckResult(
            "MGLRU min_ttl", "ok",
            f"{v} ms (non-default — operator override)",
        )
    return CheckResult("MGLRU min_ttl", "ok", "1000 ms")


_JTS_SYSCTL_CONF = Path("/etc/sysctl.d/99-jts-vm.conf")


@dataclass
class _SysctlConf:
    """Result of parsing the JTS sysctl drop-in.

    `values` — vm.* keys with resolved numeric/string values.
    `unresolved` — vm.* keys whose value is an unsubstituted template
        placeholder like '__VM_MIN_FREE_KBYTES__'. A non-empty list
        means install.sh's sed step failed for that key — the kernel
        will silently use whatever it had before, and the doctor
        should warn so the operator knows their config is broken."""
    values: dict[str, str]
    unresolved: list[str]


def _parse_jts_sysctl_conf() -> _SysctlConf:
    """Parse the JTS sysctl drop-in. Key (after `vm.`) maps to the
    resolved value if it's a real value, or lands in `unresolved` if
    the template substitution failed."""
    values: dict[str, str] = {}
    unresolved: list[str] = []
    if not _JTS_SYSCTL_CONF.exists():
        return _SysctlConf(values=values, unresolved=unresolved)
    try:
        for line in _JTS_SYSCTL_CONF.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if not key.startswith("vm."):
                continue
            # Drop any 'vm.' prefix — we'll match against /proc/sys/vm/<key>.
            sub_key = key[3:]
            # Template placeholder (install.sh's sed step failed)?
            if value.startswith("__") and value.endswith("__"):
                unresolved.append(sub_key)
                continue
            values[sub_key] = value
    except OSError:
        return _SysctlConf(values={}, unresolved=[])
    return _SysctlConf(values=values, unresolved=unresolved)


def check_sysctl_drift() -> CheckResult:
    """Verify the vm.* tunings from /etc/sysctl.d/99-jts-vm.conf
    took effect. Drift detection — not a failure, just informational
    so the operator knows whether to re-apply via `sudo sysctl --system`
    or reboot.

    Reads expected values from the installed conf file rather than
    hardcoding, so RAM-dependent values (vm.min_free_kbytes, which
    install.sh computes per-Pi as 2% of RAM) are checked against
    the right target for THIS hardware. On systems with no
    /proc/sys/vm/ at all (e.g. running the doctor in a dev
    container), skip cleanly."""
    conf = _parse_jts_sysctl_conf()
    if not conf.values and not conf.unresolved:
        return CheckResult(
            "vm.* sysctls", "warn",
            f"{_JTS_SYSCTL_CONF} missing or empty — re-run install.sh",
        )
    # Unresolved template placeholders are a higher-priority warning
    # than drift — they mean install.sh's sed step failed and the
    # operator is running with kernel defaults for those knobs (not
    # what they wanted).
    if conf.unresolved:
        return CheckResult(
            "vm.* sysctls", "warn",
            "unsubstituted template placeholder(s) in conf: " +
            ", ".join(f"vm.{k}" for k in conf.unresolved) +
            ". install.sh's sed step likely failed — re-run install.sh.",
        )
    expected = conf.values
    drift = []
    checked = 0
    for key, want in expected.items():
        path = Path(f"/proc/sys/vm/{key}")
        if not path.exists():
            continue  # kernel doesn't expose this knob
        try:
            got = path.read_text().strip()
        except OSError:
            continue
        checked += 1
        if got != want:
            drift.append(f"vm.{key}={got} (want {want})")
    if checked == 0:
        return CheckResult(
            "vm.* sysctls", "ok", "/proc/sys/vm not available (not Linux?)",
        )
    if drift:
        return CheckResult(
            "vm.* sysctls", "warn",
            "drift: " + ", ".join(drift) +
            ". Run `sudo sysctl --system` or check /etc/sysctl.d/99-jts-vm.conf.",
        )
    return CheckResult(
        "vm.* sysctls", "ok", f"all {checked} expected values live",
    )


# Expected StartLimitAction= per critical daemon. T5.1 of the
# watchdog-liveness plan: a restart spiral on any of these four
# escalates to a clean system reboot rather than waiting for the
# Tier 5 kernel hardware watchdog (which has the "PID 1 alive but
# userspace dead" blind spot documented in HANDOFF-resilience.md).
# Doctor reports drift so a Debian/RPi-OS update that removes our
# unit-file directives surfaces in the next install. See
# docs/HANDOFF-tier5-watchdog-liveness.md "Option B (T5.1)".
_EXPECTED_START_LIMIT_ACTION = {
    "jasper-camilla": "reboot",
    "jasper-aec-bridge": "reboot",
    "jasper-voice": "reboot",
    "jasper-control": "reboot",
}


# OOMScoreAdjust values are the canonical set from jasper._oom_adj —
# shared with install.sh so a future tweak only touches one file.
# See jasper/_oom_adj.py for rationale per daemon.
from .._oom_adj import EXPECTED as _EXPECTED_OOM_ADJ  # noqa: E402


def _pid_of_unit(unit: str) -> int | None:
    """Best-effort single-unit PID lookup. Returns None if the unit
    isn't running, or if systemctl isn't available (dev host).

    Used only when a caller wants just one PID. The batch caller
    `check_oom_score_adj` uses `_systemctl_show_property` directly
    to avoid N subprocess invocations for N units."""
    try:
        out = _run(
            ["systemctl", "show", "-p", "MainPID", "--value", f"{unit}.service"],
        ).stdout.strip()
        pid = int(out)
        return pid if pid > 0 else None
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        return None


def _systemctl_show_property(prop: str, units: list[str]) -> list[str] | None:
    """Batch read of one systemd property across multiple units. One
    subprocess call returns N values (one per unit, in input order).

    Returns:
        list of values (length == len(units)), OR None if systemctl
        is unavailable (dev host).

    Why this matters: before the batch, check_oom_score_adj called
    `systemctl show` 2× per daemon × 7 daemons = 14 subprocess
    invocations per doctor run. With the batch, it's 2 invocations
    total — one for MainPID, one for OOMScoreAdjust. ~7× faster
    per check.

    Wire format note: `systemctl show -p X --value <u1> <u2> ... <uN>`
    emits `value1\\n\\nvalue2\\n\\n...valueN\\n`. The separator is
    `\\n\\n` (blank line between values), NOT plain `\\n`. We split
    on that explicitly.
    """
    try:
        out = _run(
            ["systemctl", "show", "-p", prop, "--value"] +
            [f"{u}.service" for u in units],
            # Wider timeout — listing N units takes longer than 1.
            timeout=10.0,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    # Strip trailing newline before splitting so the last value isn't
    # followed by a phantom empty element.
    text = out.rstrip("\n")
    # systemctl separates per-unit values with a blank line (\n\n) when
    # multiple units are requested with --value. Splitting on \n alone
    # would produce 2N-1 elements for N units; split on \n\n to get N.
    if not text:
        # All units returned empty values (e.g. all not-running).
        # Still need len(units) entries.
        return [""] * len(units)
    if "\n\n" in text:
        parts = text.split("\n\n")
    else:
        # Single unit, or systemd version that doesn't emit blank
        # separators. Fall back to plain \n split.
        parts = text.split("\n")
    if len(parts) != len(units):
        # Unexpected shape — degrade gracefully so the caller can
        # surface "skipped" rather than crash.
        return None
    return parts


def check_oom_score_adj() -> CheckResult:
    """Verify critical daemons have the OOMScoreAdjust we configured.
    Checks BOTH the live process (/proc/<pid>/oom_score_adj — the
    kernel's actual victim-selection value) AND the unit-file value
    (systemctl show -p OOMScoreAdjust — what's set for the NEXT
    restart). Live drift means a process was started before the
    new unit landed → next restart fixes it. Configured drift means
    the unit file itself doesn't have the directive → next restart
    *won't* fix it, so we surface both shapes separately."""
    units = list(_EXPECTED_OOM_ADJ.keys())
    # Batch both systemctl-show calls — one subprocess per property
    # instead of one per (property × unit).
    pids_raw = _systemctl_show_property("MainPID", units)
    configs_raw = _systemctl_show_property("OOMScoreAdjust", units)
    if pids_raw is None or configs_raw is None:
        return CheckResult(
            "OOM score adj", "ok",
            "systemctl unavailable — skipped (not Linux?)",
        )
    live_drift = []   # /proc/PID disagrees with expected
    config_drift = []  # systemctl show disagrees with expected
    missing = []
    for unit, want, pid_str, config_str in zip(
        units, _EXPECTED_OOM_ADJ.values(), pids_raw, configs_raw,
    ):
        # Parse configured value. systemd returns "0" when
        # OOMScoreAdjust= is absent from the unit (its default).
        try:
            configured = int(config_str) if config_str else 0
        except ValueError:
            configured = None
        if configured is not None and configured != want:
            config_drift.append(f"{unit} unit={configured} (want {want})")
        # Parse PID. systemctl returns "0" when the unit isn't running.
        try:
            pid = int(pid_str) if pid_str else 0
        except ValueError:
            pid = 0
        if pid <= 0:
            missing.append(unit)
            continue
        try:
            got = int(Path(f"/proc/{pid}/oom_score_adj").read_text().strip())
        except (OSError, ValueError):
            continue
        if got != want:
            live_drift.append(f"{unit} live={got} (want {want})")
    if config_drift:
        # Unit-file drift is the more serious case — survives restarts.
        return CheckResult(
            "OOM score adj", "warn",
            "UNIT FILE drift (next restart won't fix): " +
            ", ".join(config_drift) +
            ". Re-run install.sh to restore .service files.",
        )
    if live_drift:
        return CheckResult(
            "OOM score adj", "warn",
            "live-process drift (will fix on next restart): " +
            ", ".join(live_drift) +
            ". `systemctl restart <unit>` to apply now.",
        )
    if missing:
        return CheckResult(
            "OOM score adj", "ok",
            f"{len(_EXPECTED_OOM_ADJ) - len(missing)} daemons protected; "
            f"{len(missing)} not running ({', '.join(missing)})",
        )
    return CheckResult(
        "OOM score adj", "ok",
        f"all {len(_EXPECTED_OOM_ADJ)} critical daemons protected",
    )


def _start_limit_action_of_unit(unit: str) -> str | None:
    """Best-effort: read `StartLimitAction=` from systemd's view of
    the unit. Returns the lowercased action string, or `None` if
    systemctl isn't available (dev host) or the lookup fails."""
    try:
        out = _run(
            ["systemctl", "show", "-p", "StartLimitAction", "--value",
             f"{unit}.service"],
        ).stdout.strip().lower()
        return out or "none"
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def check_start_limit_action() -> CheckResult:
    """Verify the T5.1 `StartLimitAction=reboot` directive is in
    effect on every critical daemon. Drift here means a Debian /
    RPi-OS update edited the unit, or someone manually disabled the
    escalation. Doctor surfaces this — without StartLimitAction=reboot
    we're back to Tier 5's "PID 1 alive but userspace dead" gap.
    See docs/HANDOFF-tier5-watchdog-liveness.md."""
    drift = []
    for unit, want in _EXPECTED_START_LIMIT_ACTION.items():
        got = _start_limit_action_of_unit(unit)
        if got is None:
            # systemctl unavailable (dev host) — skip cleanly
            return CheckResult(
                "StartLimitAction", "ok",
                "systemctl unavailable — skipped (not Linux?)",
            )
        if got != want:
            drift.append(f"{unit}={got} (want {want})")
    if drift:
        return CheckResult(
            "StartLimitAction", "warn",
            "T5.1 escalation drift: " + ", ".join(drift) +
            ". Re-run install.sh to restore .service files.",
        )
    return CheckResult(
        "StartLimitAction", "ok",
        f"T5.1 reboot escalation active on all {len(_EXPECTED_START_LIMIT_ACTION)} "
        "critical daemons",
    )


# --- Stage 2 audio-protection checks (shipped 2026-05-24) ---
#
# These verify that the audio-path daemons' pages won't be swapped to
# zram under memory pressure — the failure mode confirmed empirically
# by the 2026-05-24 stress test (splotchy/crushed music as zram
# decompression jitter blew the ALSA buffer timing budget).


def check_cgroup_memory_enabled() -> CheckResult:
    """Verify the Linux memory cgroup controller is actually enabled.
    Required for `MemorySwapMax=0` on jts-audio.slice / jts-mic.slice
    to enforce. The Pi 5 DTB defaults to `cgroup_disable=memory`;
    install.sh adds `cgroup_enable=memory` to cmdline.txt to override.
    Failure here means the audio-slice protection is silently a
    no-op — exactly the trap PR1 + PR1.6 documented for the existing
    `MemoryHigh=`/`MemoryMax=` directives."""
    p = Path("/sys/fs/cgroup/cgroup.controllers")
    if not p.exists():
        return CheckResult(
            "cgroup memory", "ok",
            "/sys/fs/cgroup not present (not Linux?)",
        )
    try:
        controllers = p.read_text().strip().split()
    except OSError:
        return CheckResult(
            "cgroup memory", "warn", "couldn't read cgroup.controllers",
        )
    if "memory" not in controllers:
        return CheckResult(
            "cgroup memory", "fail",
            "memory controller NOT enabled — audio-slice MemorySwapMax=0 "
            "is silently a no-op. Reboot to apply install.sh's cmdline.txt "
            "edit (cgroup_enable=memory).",
        )
    return CheckResult(
        "cgroup memory", "ok",
        "controller enabled (audio-slice protection effective)",
    )


# Audio-path daemons that should NEVER accumulate VmSwap. The check
# is permissive about small transient values (kernel sometimes evicts
# a few pages during process startup) but warns if any daemon has
# meaningful swap — that's the 2026-05-24 failure-mode signature.
_AUDIO_PATH_UNITS = (
    "jasper-fanin",
    "jasper-outputd",
    "jasper-camilla",
    "jasper-aec-bridge",
    "shairport-sync",
    "librespot",
    "bluealsa-aplay",
)

# Threshold for "this daemon has meaningful pages in zram" — well above
# the small (<100 kB) transient that's normal at startup, well below
# the 42 MB observed on aec-bridge during the 2026-05-24 stress.
_AUDIO_VMSWAP_WARN_KB = 1024  # 1 MB


def check_audio_path_no_swap() -> CheckResult:
    """Verify audio-path daemons have ~0 pages in zram. If any are
    swapped meaningfully (>1 MB), it means either the slice's
    `MemorySwapMax=0` isn't enforcing (cgroup memory not enabled,
    Slice= not assigned, or daemon not in the slice) — OR pressure
    has already started evicting audio pages, in which case music
    quality is at risk."""
    swapped: list[str] = []
    missing: list[str] = []
    for unit in _AUDIO_PATH_UNITS:
        pid = _pid_of_unit(unit)
        if pid is None:
            missing.append(unit)
            continue
        try:
            status = Path(f"/proc/{pid}/status").read_text()
        except OSError:
            continue
        vmswap_kb = 0
        for line in status.split("\n"):
            if line.startswith("VmSwap:"):
                try:
                    vmswap_kb = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
                break
        if vmswap_kb > _AUDIO_VMSWAP_WARN_KB:
            swapped.append(f"{unit}={vmswap_kb} kB")
    if swapped:
        return CheckResult(
            "audio path no-swap", "warn",
            "audio-path daemons with pages in zram: " +
            ", ".join(swapped) +
            ". Check Slice= and cgroup_enable=memory; music may glitch "
            "under load until restored.",
        )
    if missing:
        running = len(_AUDIO_PATH_UNITS) - len(missing)
        return CheckResult(
            "audio path no-swap", "ok",
            f"{running} audio daemons running, all swap-free; "
            f"{len(missing)} not running ({', '.join(missing)})",
        )
    return CheckResult(
        "audio path no-swap", "ok",
        f"all {len(_AUDIO_PATH_UNITS)} audio-path daemons swap-free",
    )


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


def _wake_leg_setting(key: str, default: bool) -> bool:
    """Read a JASPER_WAKE_LEG_* boolean from aec_mode.env, with the
    same normalization the bash reconciler does. Defaults applied when
    the file is missing, the key is missing, or the value is malformed
    — matches install.sh's reconcile_aec_state seeds."""
    p = Path("/var/lib/jasper/aec_mode.env")
    if not p.exists():
        return default
    try:
        for line in p.read_text().split("\n"):
            line = line.strip()
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip().strip("'\"").lower()
                if val in ("1", "on", "true", "yes", "y",
                           "enabled", "enable"):
                    return True
                if val in ("0", "off", "false", "no", "n",
                           "disabled", "disable", ""):
                    return False
                return default
    except OSError:
        pass
    return default


def check_wake_legs_configured() -> CheckResult:
    """Reports which additive wake-detection legs are armed via the
    /system Wake detection card (raw chip-direct + DTLN neural). The
    AEC3 master leg is reported separately by check_aec_bridge_running.

    Skips cleanly if AEC is disabled — leg booleans are meaningless
    without the bridge emitting on the UDP ports they consume."""
    aec_mode = _aec_mode_setting()
    if aec_mode != "auto":
        return CheckResult(
            "Wake legs", "ok",
            f"n/a — AEC mode is {aec_mode}; additive legs require AEC on",
        )
    raw = _wake_leg_setting("JASPER_WAKE_LEG_RAW", True)
    dtln = _wake_leg_setting("JASPER_WAKE_LEG_DTLN", False)
    armed = [name for name, on in
             (("aec3", True), ("raw", raw), ("dtln", dtln))
             if on]
    detail = (
        f"{len(armed)} leg(s) armed: {', '.join(armed)}. "
        f"Toggle at http://jts.local/system (Wake detection card)."
    )
    return CheckResult("Wake legs", "ok", detail)


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

    In fan-in topology, substream 7 is jasper-fanin's summed output and
    may be open even when every renderer is idle. Count only input
    lanes 0..4 for "music active" so AEC output health does not
    confuse the daemon's own output with a renderer source.

    Used to gate the AEC bridge FAIL: ref-silent windows are only
    diagnostic of a broken dsnoop when music IS being routed through the
    loopback. When no renderer is writing, ref-silent is the expected
    state and mic-loud bursts come from non-loopback sources (TTS via
    jasper_out, voice in the room).
    """
    import glob
    for status_path in glob.glob("/proc/asound/Loopback/pcm0p/sub*/status"):
        m = re.search(r"/sub(\d+)/status$", status_path)
        if m and int(m.group(1)) > 4:
            continue
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
            f"reference. In the fan-in topology, first verify "
            f"/etc/asound.conf maps pcm.jasper_capture to hw:Loopback,1,7 "
            f"(jasper-fanin's summed output) and that jasper-fanin is "
            f"active. A stale dmix-era capture tap on substream 0 can make "
            f"jasper_ref busy or silent. See docs/HANDOFF-aec.md "
            f"Lessons learned for the original silent-ref failure mode.",
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


def check_fanin_binary_installed() -> CheckResult:
    """The jasper-fanin Rust daemon ships as an installed binary at
    /opt/jasper/bin/jasper-fanin. install.sh runs cargo build during
    deploy; this check verifies the build actually produced the
    binary. A missing binary means cargo build silently failed and
    renderer audio cannot run.

    See docs/HANDOFF-fan-in-daemon.md for the design.
    """
    path = Path("/opt/jasper/bin/jasper-fanin")
    if not path.exists():
        return CheckResult(
            "jasper-fanin binary",
            "fail",
            f"{path} missing. Re-run install.sh; check cargo build "
            f"output for compilation errors.",
        )
    if not os.access(path, os.X_OK):
        return CheckResult(
            "jasper-fanin binary",
            "fail",
            f"{path} present but not executable. Run: "
            f"sudo chmod +x {path}",
        )
    try:
        size_kb = path.stat().st_size // 1024
    except OSError:
        size_kb = 0
    return CheckResult(
        "jasper-fanin binary", "ok", f"{path} ({size_kb} KB)"
    )


def _asound_non_comment_text(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _asound_pcm_block(text: str, name: str) -> str | None:
    """Return a top-level pcm.NAME block body from an asoundrc.

    The deployed ALSA snippets keep each top-level PCM block separated
    by the next `pcm.` or `ctl.` definition. We do not need a full ALSA
    parser here; this is a drift detector for our own generated file.
    """
    pattern = re.compile(rf"^pcm\.{re.escape(name)}\s*\{{", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    tail = text[match.start():]
    next_def = re.search(r"^(?:pcm|ctl)\.", tail[match.end() - match.start():], re.MULTILINE)
    if next_def:
        return tail[:match.end() - match.start() + next_def.start()]
    return tail


_FANIN_EXPECTED_INPUTS = [
    ("spotify", "hw:Loopback,1,0"),
    ("airplay", "hw:Loopback,1,1"),
    ("bluealsa", "hw:Loopback,1,2"),
    ("usbsink", "hw:Loopback,1,3"),
    ("correction", "hw:Loopback,1,4"),
]
_FANIN_EXPECTED_OUTPUT_PCM = "hw:Loopback,0,7"
_OUTPUTD_EXPECTED_CONTENT_PCM = "outputd_content_capture"
_OUTPUTD_EXPECTED_DAC_PCM = "outputd_dac"
_OUTPUTD_STATUS_SOCKET = "/run/jasper-outputd/control.sock"


def check_fanin_asound_wiring() -> CheckResult:
    """Verify the deployed ALSA graph is the fan-in graph.

    This catches the exact split-brain failure that can break AEC:
    renderers and jasper-fanin are running in fan-in mode, but
    /etc/asound.conf still points `pcm.jasper_capture` at the old
    substream 0 instead of the summed fan-in output on substream 7.
    """
    label = "fan-in ALSA wiring"
    path = Path("/etc/asound.conf")
    if not path.exists():
        return CheckResult(label, "fail", f"{path} missing — re-run install.sh")
    try:
        text = path.read_text()
    except OSError as e:
        return CheckResult(label, "fail", f"can't read {path}: {e}")

    active = _asound_non_comment_text(text)
    legacy_blocks = [
        name for name in ("jasper_renderer_mix", "jasper_renderer_in")
        if re.search(rf"^pcm\.{name}\s*\{{", active, re.MULTILINE)
    ]
    if legacy_blocks:
        return CheckResult(
            label,
            "fail",
            f"{path} still defines legacy renderer dmix block(s): "
            f"{', '.join(legacy_blocks)}. Fan-in-only installs must "
            f"define private renderer lanes and no jasper_renderer_* "
            f"front end. Re-run deploy/install.sh.",
        )

    expected_aliases = {
        "librespot_substream": "hw:Loopback,0,0",
        "shairport_substream": "hw:Loopback,0,1",
        "bluealsa_substream": "hw:Loopback,0,2",
        "usbsink_substream": "hw:Loopback,0,3",
        "correction_substream": "hw:Loopback,0,4",
    }
    missing: list[str] = []
    wrong: list[str] = []
    for alias, slave in expected_aliases.items():
        block = _asound_pcm_block(active, alias)
        if block is None:
            missing.append(alias)
        elif (
            f'pcm "{slave}"' not in block
            or "rate 48000" not in block
            or "channels 2" not in block
            or "format S16_LE" not in block
        ):
            wrong.append(f"{alias}≠{slave}")
    if missing or wrong:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if wrong:
            parts.append("wrong slave " + ", ".join(wrong))
        return CheckResult(
            label,
            "fail",
            "; ".join(parts) + ". Re-run deploy/install.sh to restore "
            "the fan-in asoundrc.",
        )

    capture = _asound_pcm_block(active, "jasper_capture")
    if capture is None:
        return CheckResult(
            label,
            "fail",
            "pcm.jasper_capture missing — CamillaDSP and AEC bridge "
            "have no shared reference tap.",
        )
    if 'pcm "hw:Loopback,1,7"' not in capture:
        detail = (
            "pcm.jasper_capture must dsnoop hw:Loopback,1,7 "
            "(jasper-fanin's summed output)."
        )
        if 'pcm "hw:Loopback,1,0"' in capture:
            detail += (
                " It currently points at substream 0, which is now a "
                "private fan-in input lane and can make jasper_ref fail "
                "with EBUSY."
            )
        return CheckResult(label, "fail", detail)
    for required in ("rate 48000", "channels 2", "format S16_LE"):
        if required not in capture:
            return CheckResult(
                label,
                "fail",
                "pcm.jasper_capture must pin the dsnoop slave to "
                f"48 kHz stereo S16_LE; missing {required!r}.",
            )

    ref = _asound_pcm_block(active, "jasper_ref")
    if ref is None:
        return CheckResult(
            label,
            "fail",
            "pcm.jasper_ref missing — AEC bridge opens jasper_ref, not "
            "jasper_capture directly.",
        )
    if 'slave.pcm "jasper_capture"' not in ref:
        return CheckResult(
            label,
            "fail",
            "pcm.jasper_ref must plug-wrap pcm.jasper_capture so the AEC "
            "bridge reads the summed fan-in reference.",
        )

    stale_state = Path("/var/lib/jasper/audio_topology.env")
    if stale_state.exists():
        return CheckResult(
            label,
            "warn",
            f"fan-in asoundrc is correct, but stale {stale_state} still "
            f"exists from the retired dmix/fanin switcher. Re-run "
            f"deploy/install.sh to archive/remove it.",
        )

    return CheckResult(
        label,
        "ok",
        "renderer/test lanes 0..4; jasper_capture/jasper_ref on summed substream 7",
    )


def check_fanin_service() -> CheckResult:
    """The jasper-fanin systemd unit is required for renderer audio.

    Fan-in is the only supported renderer topology. If the daemon is
    disabled or inactive, AirPlay/Spotify/Bluetooth/USB-in may write to
    their private lanes, but nothing publishes the summed stream to
    CamillaDSP or the AEC bridge.

    Returns:
      - ok ("active, responding") when enabled and the UDS endpoint
        replies to STATUS with a fresh progress sentinel.
      - fail when disabled/inactive, when STATUS cannot be read, or
        when the live STATUS schema drifts from the production graph.
      - warn when enabled+active but the work loop is stale.
    """
    enabled = _run(
        ["systemctl", "is-enabled", "jasper-fanin.service"]
    ).stdout.strip()
    active = _run(
        ["systemctl", "is-active", "jasper-fanin.service"]
    ).stdout.strip()

    if enabled in ("disabled", "static", "indirect"):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"state={enabled}. Fan-in is mandatory; run: "
            f"sudo systemctl enable --now jasper-fanin.service",
        )
    if enabled == "not-found":
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "systemd unit not installed. Re-run install.sh.",
        )

    # Unit is enabled (or masked, alias, ...) — operator opted in.
    if active != "active":
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"enabled but state={active}. "
            f"Check: journalctl -u jasper-fanin",
        )

    # Service is active. Probe the UDS endpoint to verify the work
    # loop is making progress (catches "process alive but wedged").
    socket_path = "/run/jasper-fanin/control.sock"
    last_error: OSError | None = None
    for attempt in range(2):
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(socket_path)
            sock.sendall(b"STATUS\n")
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            sock.close()
            break
        except OSError as e:
            last_error = e
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            if attempt == 0:
                time.sleep(0.1)
    else:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but UDS probe at {socket_path} failed: {last_error}. "
            f"Fan-in is mandatory; without STATUS doctor cannot verify "
            f"the live graph, buffers, or watchdog progress. "
            f"check: journalctl -u jasper-fanin | tail",
        )

    body = b"".join(chunks).decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but UDS STATUS returned invalid JSON: {e}",
        )

    output_pcm = data.get("output", {}).get("pcm")
    if output_pcm != _FANIN_EXPECTED_OUTPUT_PCM:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but STATUS output.pcm={output_pcm!r}; expected "
            f"{_FANIN_EXPECTED_OUTPUT_PCM}. Check /var/lib/jasper/fanin.env.",
        )
    inputs = data.get("inputs")
    if not isinstance(inputs, list):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS response missing inputs[]",
        )
    actual_inputs = [
        (inp.get("label"), inp.get("pcm"))
        for inp in inputs
        if isinstance(inp, dict)
    ]
    if actual_inputs != _FANIN_EXPECTED_INPUTS:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS inputs drifted. Expected "
            f"{_FANIN_EXPECTED_INPUTS!r}; got {actual_inputs!r}. "
            "Check /var/lib/jasper/fanin.env.",
        )

    progress_age = data.get("watchdog", {}).get(
        "last_progress_age_ms", -1
    )
    if not isinstance(progress_age, (int, float)) or progress_age < 0:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS response missing watchdog state",
        )
    if progress_age > 1000:
        return CheckResult(
            "jasper-fanin service",
            "warn",
            f"active but last_progress_age_ms={progress_age} "
            f"(work loop may be wedged; watchdog should fire soon)",
        )
    frames = data.get("output", {}).get("frames_written", 0)
    xruns = data.get("output", {}).get("xrun_count", 0)
    input_buffer_frames = data.get("input_buffer_frames")
    output_buffer_frames = data.get("output", {}).get("buffer_frames")
    if not isinstance(input_buffer_frames, int):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS missing integer input_buffer_frames",
        )
    if not isinstance(output_buffer_frames, int):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS missing integer output.buffer_frames",
        )
    input_xruns = []
    for inp in data.get("inputs", []):
        try:
            count = int(inp.get("xrun_count", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        if count:
            input_xruns.append(f"{inp.get('label', '?')}={count}")
    if input_buffer_frames < 4096:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active, but runtime input_buffer_frames={input_buffer_frames} is below "
            f"4096. AirPlay WiFi burst absorption was validated at 4096; "
            f"check /var/lib/jasper/fanin.env and "
            f"JASPER_FANIN_INPUT_BUFFER_FRAMES.",
        )
    if output_buffer_frames < 3072:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active, but runtime output_buffer_frames={output_buffer_frames} is below "
            f"3072. CamillaDSP short-read warnings were observed with "
            f"1024 and 2048-frame fan-in output buffers; production is "
            f"validated at 3072. Check /var/lib/jasper/fanin.env and "
            f"JASPER_FANIN_OUTPUT_BUFFER_FRAMES.",
        )
    if output_buffer_frames > 3072:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active, but runtime output_buffer_frames={output_buffer_frames} exceeds "
            f"3072. Larger fan-in output queues add latency and need a "
            f"fresh AirPlay offset validation before shipping. "
            f"Check /var/lib/jasper/fanin.env and "
            f"JASPER_FANIN_OUTPUT_BUFFER_FRAMES.",
        )
    return CheckResult(
        "jasper-fanin service",
        "ok",
        f"active, frames_written={frames}, "
        f"input_buffer_frames={input_buffer_frames}, "
        f"output_buffer_frames={output_buffer_frames}, "
        f"output xruns={xruns}, input xruns={','.join(input_xruns) or '0'}, "
        f"progress_age_ms={progress_age}",
    )


def check_outputd_service() -> CheckResult:
    """Validate the outputd final-output-owner daemon.

    This cutover branch expects outputd to own the physical DAC. Treat
    disabled/inactive outputd as a real audio-path failure and verify
    the STATUS socket, runtime backend, negotiated buffers, xrun
    counters, and progress sentinel.
    """
    enabled = _run(
        ["systemctl", "is-enabled", "jasper-outputd.service"]
    ).stdout.strip()
    if enabled == "not-found":
        return CheckResult(
            "jasper-outputd",
            "fail",
            "systemd unit is not installed. Re-run install.sh.",
        )
    if enabled not in {"enabled", "static"}:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"systemd unit is {enabled or 'unknown'}; expected enabled "
            "on the outputd cutover branch.",
        )
    active = _run(
        ["systemctl", "is-active", "jasper-outputd.service"]
    ).stdout.strip()
    if active != "active":
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"service state={active or 'unknown'}. "
            "Check: journalctl -u jasper-outputd",
        )

    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(_OUTPUTD_STATUS_SOCKET)
        sock.sendall(b"STATUS\n")
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as e:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but STATUS probe at {_OUTPUTD_STATUS_SOCKET} failed: {e}. "
            "Without STATUS doctor cannot verify DAC ownership, buffers, "
            "xruns, or work-loop progress.",
        )
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    body = b"".join(chunks).decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but STATUS returned invalid JSON: {e}",
        )

    if data.get("backend") != "alsa":
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but backend={data.get('backend')!r}; expected 'alsa'",
        )
    content = data.get("content", {})
    dac = data.get("dac", {})
    if content.get("pcm") != _OUTPUTD_EXPECTED_CONTENT_PCM:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"content.pcm={content.get('pcm')!r}; expected "
            f"{_OUTPUTD_EXPECTED_CONTENT_PCM!r}",
        )
    if dac.get("pcm") != _OUTPUTD_EXPECTED_DAC_PCM:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"dac.pcm={dac.get('pcm')!r}; expected {_OUTPUTD_EXPECTED_DAC_PCM!r}",
        )
    sample_rate = dac.get("sample_rate")
    period_frames = dac.get("period_frames")
    content_buffer = content.get("buffer_frames")
    dac_buffer = dac.get("buffer_frames")
    if sample_rate != 48000:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"dac.sample_rate={sample_rate!r}; expected 48000",
        )
    if not isinstance(period_frames, int) or period_frames <= 0:
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS missing positive dac.period_frames",
        )
    if not isinstance(content_buffer, int) or content_buffer < period_frames * 2:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"content.buffer_frames={content_buffer!r}; expected >= "
            f"2 x period ({period_frames})",
        )
    if not isinstance(dac_buffer, int) or dac_buffer < period_frames * 2:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"dac.buffer_frames={dac_buffer!r}; expected >= "
            f"2 x period ({period_frames})",
        )

    progress_age = data.get("watchdog", {}).get(
        "last_progress_age_ms", -1
    )
    if not isinstance(progress_age, (int, float)) or progress_age < 0:
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS response missing watchdog.last_progress_age_ms",
        )
    content_xruns = int(content.get("xrun_count", 0) or 0)
    dac_xruns = int(dac.get("xrun_count", 0) or 0)
    content_empty = int(content.get("empty_periods", 0) or 0)
    content_partial = int(content.get("partial_periods", 0) or 0)
    content_eagain = int(content.get("eagain_count", 0) or 0)
    frames = int(dac.get("frames_written", 0) or 0)
    tts = data.get("tts", {})
    tts_pending = int(tts.get("pending_frames", 0) or 0)
    tts_over_budget = bool(tts.get("over_budget", False))
    tts_over_budget_ms = int(tts.get("over_budget_ms", 0) or 0)
    tts_over_budget_streak_ms = int(
        tts.get("over_budget_streak_ms", 0) or 0
    )
    tts_max_pending = int(tts.get("max_pending_frames", 0) or 0)
    if progress_age > 1000:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but last_progress_age_ms={progress_age} "
            "(work loop may be wedged; watchdog should fire soon)",
        )
    if tts_over_budget or tts_pending > 48000 * 2:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but tts.pending_frames={tts_pending} (>2s). "
            f"over_budget_streak_ms={tts_over_budget_streak_ms}. "
            "TTS producer may be outrunning outputd playback.",
        )
    return CheckResult(
        "jasper-outputd",
        "ok",
        f"active, backend=alsa, frames_written={frames}, "
        f"content_buffer_frames={content_buffer}, dac_buffer_frames={dac_buffer}, "
        f"xruns={content_xruns}/{dac_xruns}, "
        f"content_empty_periods={content_empty}, "
        f"content_partial_periods={content_partial}, "
        f"content_eagain_count={content_eagain}, "
        f"tts_pending_frames={tts_pending}, "
        f"tts_max_pending_frames={tts_max_pending}, "
        f"tts_over_budget_ms={tts_over_budget_ms}, "
        f"progress_age_ms={progress_age}",
    )


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


def _assess_dtln_engine(journal_text: str) -> CheckResult:
    """Pure-function parser for the bridge's DTLN-aec engine init
    line. Split out from `check_aec_bridge_dtln_engine` so the
    parsing logic is unit-testable without subprocess mocks.

    Successful load line shape (jasper/cli/aec_bridge.py ~line 675):
        DTLN-aec engine enabled: size=256, udp out=...
    Failed load line shape:
        JASPER_AEC_DTLN_ENABLED set but DTLN couldn't load: <reason>.
        Continuing with AEC3 only.
    """
    # Search newest-first — we want the most recent engine init,
    # not the first one in the window (which may predate a restart).
    for line in reversed(journal_text.splitlines()):
        if "DTLN-aec engine enabled" in line:
            size = "?"
            if "size=" in line:
                size = line.split("size=", 1)[1].split(",", 1)[0].strip()
            return CheckResult(
                "DTLN-aec engine", "ok",
                f"loaded (size={size}, triple-stream tertiary leg active)",
            )
        if "DTLN couldn't load" in line:
            detail = line.split("couldn't load:", 1)[-1].strip()
            return CheckResult(
                "DTLN-aec engine", "fail",
                f"JASPER_AEC_DTLN_ENABLED=1 but engine couldn't load: "
                f"{detail}. Bridge degraded to AEC3-only — triple-stream "
                f"is silently dual-stream. Check /var/lib/jasper/dtln/ "
                f"and `journalctl -u jasper-aec-bridge -e`.",
            )

    # Neither marker found. Either the bridge has been running long
    # enough that the init line aged out (we use a 10-min window) or
    # JASPER_AEC_DTLN_ENABLED was set after the last bridge start.
    return CheckResult(
        "DTLN-aec engine", "warn",
        "JASPER_AEC_DTLN_ENABLED=1 but no engine-init line in last "
        "10 min — bridge may not have restarted since the env var was "
        "set. Try: sudo systemctl restart jasper-aec-bridge",
    )


def check_aec_bridge_dtln_engine() -> CheckResult:
    """Verify the DTLN-aec engine (triple-stream tertiary leg) is
    actually running when `JASPER_AEC_DTLN_ENABLED=1`.

    Without this check, a silent DTLN load failure would degrade
    triple-stream to dual-stream invisibly. The wake_events DB
    would just always have NULL DTLN scores, the analyzer would
    show "DTLN never fires" (correctly — because it never ran),
    and a week of data would lead to the wrong conclusion.

    Skip cleanly when `JASPER_AEC_DTLN_ENABLED` is unset or 0 —
    that's the legacy dual-stream / single-stream path, working
    as intended. Journal parsing is delegated to
    `_assess_dtln_engine` so it can be unit-tested in isolation."""
    enabled = os.environ.get("JASPER_AEC_DTLN_ENABLED", "0").strip().lower()
    if enabled not in ("1", "true", "yes", "on"):
        return CheckResult(
            "DTLN-aec engine", "ok",
            "skipped — JASPER_AEC_DTLN_ENABLED not set (dual-stream mode)",
        )

    # Bridge must be running for the engine to mean anything.
    is_active = _run(
        ["systemctl", "is-active", "jasper-aec-bridge.service"]
    ).stdout.strip()
    if is_active != "active":
        return CheckResult(
            "DTLN-aec engine", "ok",
            "(bridge not running — see AEC bridge service check above)",
        )

    # 10-minute window covers a recent install.sh deploy + any
    # post-deploy restarts. The engine init line is logged once at
    # bridge startup, so we just need to look back far enough to
    # find the most recent startup.
    proc = _run(
        ["journalctl", "-u", "jasper-aec-bridge.service",
         "--since", "10 min ago", "--no-pager", "--output", "cat"],
        timeout=8.0,
    )
    if proc.returncode != 0:
        return CheckResult(
            "DTLN-aec engine", "warn",
            f"could not read journal: {proc.stderr.strip() or 'unknown error'}",
        )

    return _assess_dtln_engine(proc.stdout)


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
    correctly by playing a brief sine into correction_substream and
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
    # probe writes to correction_substream, a dedicated fan-in input,
    # but it still emerges from the speaker and would mix with active
    # music for 5 s.
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
        if _loopback_playback_active():
            results.append(CheckResult(
                "probe — renderers idle", "fail",
                "a fan-in input lane is currently open in /proc/asound; "
                "refuse to play test sine over active renderer audio. "
                "Stop playback and re-run.",
            ))
            return results
        results.append(CheckResult(
            "probe — renderers idle", "ok",
            f"active_source={active!r}",
        ))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        # correction_substream is a private fan-in input, so aplay won't
        # necessarily get EBUSY just because AirPlay/Spotify is active.
        # If /state is down, fall back to /proc/asound ownership before
        # deciding whether the active probe is safe to run.
        if _loopback_playback_active():
            results.append(CheckResult(
                "probe — renderers idle", "fail",
                f"jasper-control /state unreachable ({e}) and a fan-in "
                f"input lane is open in /proc/asound. Refuse to play "
                f"test sine over possible active renderer audio.",
            ))
            return results
        results.append(CheckResult(
            "probe — renderers idle", "warn",
            f"jasper-control /state unreachable ({e}); /proc/asound "
            f"shows fan-in input lanes idle, so proceeding with active "
            f"probe.",
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

    # Play the sine through the dedicated correction/test fan-in lane.
    # Its plug wrapper handles format/rate conversion before fanin.
    play = _run(
        ["aplay", "-q", "-D", "correction_substream", _PROBE_SINE_PATH],
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
            f"If 'Unknown PCM', re-run install.sh so /etc/asound.conf "
            f"defines correction_substream; if 'invalid argument', check "
            f"/proc/asound/Loopback exists.",
        ))
        return results
    results.append(CheckResult(
        "probe — aplay sine", "ok",
        f"{_PROBE_SINE_DURATION_S:.0f} s of {freq} Hz sine to correction_substream",
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
            "UAC2Gadget card present (host will see the speaker as USB audio)",
        )
    return CheckResult(
        "usbsink card", "fail",
        "service active but /proc/asound/UAC2Gadget missing — "
        "init.service didn't bind the UDC. Check "
        "`systemctl status jasper-usbsink-init` for the failure mode.",
    )


def check_usbsink_active_libcomposite() -> CheckResult:
    """The mirror of check_usbsink_state's RAM-drift check: when the
    daemon IS active but libcomposite is NOT loaded, the daemon will
    appear running to systemd but audio won't flow (no gadget = no
    capture endpoint). This asymmetry can happen if a user manually
    `rmmod libcomposite` while the daemon is up, or if init.service
    succeeded its modprobe but a subsequent reload unloaded the
    module. The init.service ↔ daemon PartOf= chain normally prevents
    this, but a manual override breaks the invariant."""
    if not _systemd_is_active("jasper-usbsink.service"):
        return CheckResult(
            "usbsink active+modules", "ok",
            "service disabled — module check skipped",
        )
    if _module_loaded("libcomposite"):
        return CheckResult(
            "usbsink active+modules", "ok",
            "service active, libcomposite loaded — consistent",
        )
    return CheckResult(
        "usbsink active+modules", "fail",
        "service active but libcomposite NOT loaded — audio won't "
        "flow even though the daemon appears healthy to systemd. "
        "Run `systemctl restart jasper-usbsink-init.service` to "
        "re-load the kernel module and re-bind the gadget.",
    )


def check_usbsink_preempt_port_reachable() -> CheckResult:
    """Verify the mux's `_usbsink_set_preempt` URL actually resolves
    to a listening port on the daemon. Detects copy-paste drift
    between mux.USBSINK_PREEMPT_PORT and
    preempt_listener.DEFAULT_PORT — both have env-var defaults that
    must agree at runtime. A silent mismatch means mux POSTs to
    nowhere; preempt protocol degrades to brief mixing without any
    surface error.

    Skips when usbsink is disabled. When enabled, opens a short TCP
    connect to the configured host:port and reports reachable / not.
    """
    if not _systemd_is_active("jasper-usbsink.service"):
        return CheckResult(
            "usbsink preempt port", "ok",
            "service disabled — port reachability skipped",
        )
    host = os.environ.get("JASPER_USBSINK_PREEMPT_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("JASPER_USBSINK_PREEMPT_PORT", "8781"))
    except ValueError:
        return CheckResult(
            "usbsink preempt port", "fail",
            "JASPER_USBSINK_PREEMPT_PORT is not an integer",
        )
    # Short TCP connect — 500 ms is plenty on localhost; any longer
    # and something else is wrong.
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect((host, port))
    except OSError as e:
        return CheckResult(
            "usbsink preempt port", "fail",
            f"daemon active but {host}:{port} not reachable: {e}. "
            "Mux's preempt POSTs will fail silently — check that "
            "JASPER_USBSINK_PREEMPT_PORT matches between the daemon "
            "and mux env files.",
        )
    finally:
        sock.close()
    return CheckResult(
        "usbsink preempt port", "ok",
        f"daemon listening on {host}:{port} (mux preempts will land)",
    )


def check_xvf_firmware_6ch() -> CheckResult:
    """6-ch firmware exposes raw mics on channels 2-5 of the XVF
    capture endpoint. The bridge depends on the 6-channel endpoint
    shape and reads channel 1 (ASR beam); channel 2 is the optional
    raw0 corpus leg."""
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


def _parse_iw_regdom(stdout: str) -> tuple[str | None, dict[str, str]]:
    """Return the global country plus per-phy countries from `iw reg get`.

    `iw reg get` prints a global section followed by zero or more phy
    sections. On Pi 5 brcmfmac, the phy section may report `country 99`
    even when the global regulatory domain is correctly set; Linux uses
    that alpha2 for driver-built regulatory domains whose specific ISO
    country cannot be determined. The global country is the actionable
    WLAN-country configuration for this doctor check.
    """
    global_country: str | None = None
    phy_countries: dict[str, str] = {}
    current_phy: str | None = None

    for raw in stdout.splitlines():
        line = raw.strip()
        if line == "global":
            current_phy = None
            continue
        if line.startswith("phy#"):
            current_phy = line.removeprefix("phy#")
            continue
        if not line.startswith("country "):
            continue

        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        country = parts[1].rstrip(":")
        if current_phy is None:
            if global_country is None:
                global_country = country
        elif current_phy not in phy_countries:
            phy_countries[current_phy] = country

    return global_country, phy_countries


def _format_phy_regdom_detail(phy_countries: dict[str, str]) -> str:
    if not phy_countries:
        return "no per-phy regdom reported"
    parts: list[str] = []
    for phy, country in sorted(phy_countries.items()):
        detail = f"phy{phy} country={country}"
        if country == "99":
            detail += " (driver custom/unlabeled; not actionable by itself)"
        elif country == "00":
            detail += " (world/unset; not actionable by itself)"
        parts.append(detail)
    return "; ".join(parts)


def check_wifi_regdom() -> CheckResult:
    """Verify the configured global WLAN regulatory country is known.

    Raspberry Pi OS records the intended WiFi country in cfg80211's
    global regulatory domain (normally via Pi Imager or
    `raspi-config nonint do_wifi_country`). That value controls legal
    channels, transmit power, and 5 GHz availability.

    Do not treat Pi 5 brcmfmac's per-phy `country 99: DFS-UNSET` as a
    product failure by itself. It is common for the Broadcom driver to
    expose a custom/unlabeled per-radio domain while the global country
    is valid. Actual scan suppression is detected by `/wifi/scan` from
    scan failures and kernel `Scanning suppressed: status (4)` logs,
    then repaired via `wifi_scan_repair`."""
    proc = _run(["iw", "reg", "get"], timeout=5)
    if proc.returncode != 0:
        return CheckResult(
            "WiFi reg domain", "warn",
            "iw reg get failed; can't verify WLAN country configuration",
        )

    global_country, phy_countries = _parse_iw_regdom(proc.stdout)
    if global_country is None:
        return CheckResult(
            "WiFi reg domain", "warn",
            "could not parse global regdom from `iw reg get` "
            "(no WiFi adapter? Ethernet-only Pi is fine)",
        )
    phy_detail = _format_phy_regdom_detail(phy_countries)
    if global_country in ("99", "00"):
        return CheckResult(
            "WiFi reg domain", "warn",
            f"global regdom is '{global_country}' (unset); set WLAN "
            "country with Pi Imager or `sudo raspi-config nonint "
            f"do_wifi_country <CC>`. {phy_detail}",
        )
    return CheckResult(
        "WiFi reg domain", "ok",
        f"global country={global_country}; {phy_detail}",
    )


def check_wifi_guardian() -> CheckResult:
    """Verify the WiFi profile guardian stash matches the active
    NetworkManager profile.

    The guardian (deploy/bin/jasper-wifi-guardian, run at boot via
    jasper-wifi-guardian.service) recreates a lost
    /etc/NetworkManager/system-connections/*.nmconnection from the
    wizard-owned stash at /var/lib/jasper/wifi_guardian.env. If the
    stash is missing or stale, the recovery contract is broken — even
    though WiFi is currently working. This check surfaces that drift.

    States:
      ok    — stash exists, SSID matches what NM is currently on
      warn  — stash absent while WiFi is up (open the /wifi/ wizard and
              save once to seed); OR stash present but SSID drifted from
              the active profile (operator likely connected via SSH); OR
              stash present and no WiFi is up (last guardian run failed
              to recreate, or NM also failed)
      (the check is informational — guardian status is never fail-
       blocking. The Pi is currently online or not regardless of the
       stash state; the stash exists to help the *next* boot.)

    Skipped silently when nmcli is missing — the guardian is no-op on
    those machines anyway (no NM, nothing to recover)."""
    label = "WiFi profile guardian"
    nmcli = shutil.which("nmcli")
    if nmcli is None:
        # No NetworkManager → guardian isn't applicable. Don't warn;
        # this is the headless-Ethernet-only Pi case.
        return CheckResult(label, "ok", "skipped — no nmcli on PATH")

    # Read the stash via the same module the wizard + tests use. We
    # never log the PSK from doctor; the SSID + key_mgmt are fine.
    from ..wifi_guardian_persistence import (
        DEFAULT_PATH as _STASH_DEFAULT,
        read_stash,
    )
    stash_path = os.environ.get("JASPER_WIFI_STASH_FILE", _STASH_DEFAULT)
    stash = read_stash(stash_path)

    # Probe active SSID via nmcli (same idiom as the guardian itself).
    proc = _run(
        [nmcli, "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
        timeout=5,
    )
    active_name: str | None = None
    if proc.returncode == 0:
        for raw in proc.stdout.splitlines():
            # Naive split: NM doesn't quote single colons in NAME often,
            # but bssid-style fields are filtered out by the field list.
            parts = raw.split(":", 1)
            if len(parts) == 2 and parts[1] in ("802-11-wireless", "wifi"):
                active_name = parts[0]
                break

    active_ssid: str | None = None
    if active_name:
        ssid_proc = _run(
            [nmcli, "-t", "-f", "802-11-wireless.ssid",
             "connection", "show", active_name],
            timeout=5,
        )
        if ssid_proc.returncode == 0:
            for raw in ssid_proc.stdout.splitlines():
                if raw.startswith("802-11-wireless.ssid:"):
                    val = raw.split(":", 1)[1]
                    if val:
                        active_ssid = val
                    break
        if active_ssid is None:
            active_ssid = active_name  # fallback

    if stash is None and active_ssid is None:
        # Both absent: fresh install on Ethernet, or WiFi off / never
        # configured. Nothing to recover from; nothing to warn about.
        return CheckResult(label, "ok", "no stash and no active WiFi (Ethernet-only?)")

    if stash is None and active_ssid is not None:
        return CheckResult(
            label, "warn",
            f"WiFi is up on {active_ssid!r} but no recovery stash exists. "
            f"Open http://jts.local/wifi/ and Connect once to seed "
            f"{stash_path} — until then, a dirty-shutdown filesystem loss "
            f"of /etc/NetworkManager/system-connections/ would brick "
            f"network access.",
        )

    if stash is not None and active_ssid is None:
        return CheckResult(
            label, "warn",
            f"stash points at {stash.ssid!r} but no WiFi is currently up. "
            f"Run `sudo /usr/local/sbin/jasper-wifi-guardian --reason manual` "
            f"to retry, or check `journalctl -u jasper-wifi-guardian` for "
            f"the most recent recreate attempt.",
        )

    # Both present: compare. Stash SSID drift from active SSID means the
    # operator likely switched networks via SSH (`nmcli dev wifi connect`)
    # and didn't re-save in the wizard. WiFi works today; recovery is
    # pointed at a network that may not be in range when needed.
    assert stash is not None and active_ssid is not None
    if stash.ssid == active_ssid:
        return CheckResult(
            label, "ok",
            f"stash matches active SSID ({active_ssid})",
        )
    return CheckResult(
        label, "warn",
        f"stash points at {stash.ssid!r} but WiFi is on {active_ssid!r}. "
        f"Re-save at http://jts.local/wifi/ to update the recovery stash; "
        f"otherwise a future dirty shutdown would recreate the wrong "
        f"network.",
    )


def _correction_root() -> Path:
    return Path(
        os.environ.get("JASPER_CORRECTION_ROOT", "/var/lib/jasper/correction")
    )


def check_correction_web_service() -> CheckResult:
    """Socket activation is the liveness contract for /correction/.

    The service itself is expected to be inactive after its idle
    timeout; the socket must remain active so nginx can spawn the
    wizard on demand.
    """
    socket_state = _run(
        ["systemctl", "is-active", "jasper-correction-web.socket"]
    ).stdout.strip()
    service_state = _run(
        ["systemctl", "is-active", "jasper-correction-web.service"]
    ).stdout.strip()
    if socket_state == "active":
        return CheckResult(
            "correction web", "ok",
            f"socket active; service={service_state or 'unknown'}",
        )
    if service_state == "active":
        return CheckResult(
            "correction web", "warn",
            "service active but socket inactive — current session may work, "
            "but /correction/ will not restart after idle exit",
        )
    return CheckResult(
        "correction web", "warn",
        f"socket={socket_state or 'unknown'}, service={service_state or 'unknown'}. "
        "Run `sudo systemctl enable --now jasper-correction-web.socket` "
        "or redeploy.",
    )


def check_correction_state_dirs() -> CheckResult:
    root = _correction_root()
    expected = [
        root,
        root / "sweeps",
        root / "captures",
        root / "sessions",
        root / "calibration_mics",
    ]
    missing = [str(p) for p in expected if not p.exists()]
    not_dirs = [str(p) for p in expected if p.exists() and not p.is_dir()]
    not_writable = [str(p) for p in expected if p.is_dir() and not os.access(p, os.W_OK)]
    if not_dirs:
        return CheckResult(
            "correction state dirs", "fail",
            "expected directories but found files: " + ", ".join(not_dirs),
        )
    if not_writable:
        return CheckResult(
            "correction state dirs", "fail",
            "not writable: " + ", ".join(not_writable),
        )
    if missing:
        return CheckResult(
            "correction state dirs", "warn",
            "missing: " + ", ".join(missing) + " — redeploy to create them",
        )
    return CheckResult("correction state dirs", "ok", str(root))


def _parse_camilla_statefile_config_path(path: Path) -> str | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    match = re.search(r"^\s*config_path:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip("'\"") or None


def _active_camilla_config_path() -> tuple[Path, str | None]:
    statefile = Path(
        os.environ.get(
            "JASPER_CAMILLA_STATEFILE",
            "/var/lib/camilladsp/outputd-statefile.yml",
        )
    )
    return statefile, _parse_camilla_statefile_config_path(statefile)


def _devices_volume_limit_from_text(text: str) -> float | None:
    in_devices = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw.startswith((" ", "\t")):
            in_devices = stripped == "devices:"
            continue
        if not in_devices:
            continue
        match = re.match(r"^\s+volume_limit:\s*([^#]+)", raw)
        if not match:
            continue
        value = match.group(1).strip().strip("'\"")
        if value in {"", "null", "~"}:
            return None
        return float(value)
    return None


def check_camilla_volume_limit() -> CheckResult:
    """Verify the active Camilla config has JTS's non-positive fader cap."""
    statefile, config_path = _active_camilla_config_path()
    if config_path is None:
        return CheckResult(
            "CamillaDSP volume_limit", "warn",
            f"could not read config_path from {statefile}",
        )
    path = Path(config_path)
    if not path.exists():
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"statefile points at missing config {config_path}",
        )
    try:
        limit = _devices_volume_limit_from_text(path.read_text())
    except ValueError as e:
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"invalid devices.volume_limit in {config_path}: {e}",
        )
    except OSError as e:
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"could not read {config_path}: {e}",
        )
    if limit is None:
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"{config_path} omits devices.volume_limit; CamillaDSP "
            "defaults to +50 dB",
        )
    if limit > DEFAULT_VOLUME_LIMIT_DB:
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"{config_path} sets devices.volume_limit={limit:.1f} dB "
            f"(expected <= {DEFAULT_VOLUME_LIMIT_DB:.1f} dB)",
        )
    return CheckResult(
        "CamillaDSP volume_limit", "ok",
        f"{config_path} devices.volume_limit={limit:.1f} dB",
    )


def check_correction_current_config() -> CheckResult:
    from jasper.correction.session import parse_current_correction

    statefile, config_path = _active_camilla_config_path()
    if config_path is None:
        return CheckResult(
            "current correction", "warn",
            f"could not read config_path from {statefile}",
        )
    path = Path(config_path)
    if not path.exists():
        return CheckResult(
            "current correction", "fail",
            f"CamillaDSP statefile points at missing config {config_path}",
        )
    parsed = parse_current_correction(str(path), config_dir=path.parent)
    if parsed is None:
        if path == Path("/etc/camilladsp/outputd-cutover.yml"):
            return CheckResult("current correction", "ok", "flat base config")
        return CheckResult(
            "current correction", "warn",
            f"custom/non-JTS config loaded: {config_path}",
        )
    return CheckResult(
        "current correction", "ok",
        f"session={parsed['session_id']} peqs={parsed['peq_count']} "
        f"({config_path})",
    )


def _sound_profile_path() -> Path:
    return Path(
        os.environ.get(
            "JASPER_SOUND_PROFILE_PATH",
            "/var/lib/jasper/sound_profile.json",
        )
    )


def check_sound_profile() -> CheckResult:
    from jasper.sound.profile import (
        SoundProfile,
        build_sound_filters,
        estimate_headroom_db,
    )

    path = _sound_profile_path()
    if not path.exists():
        return CheckResult(
            "sound profile",
            "ok",
            "default Flat profile (no saved preference EQ)",
        )
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return CheckResult("sound profile", "fail", f"could not read {path}: {e}")

    profile = SoundProfile.from_mapping(raw)
    filter_count = len(build_sound_filters(profile))
    headroom_db = estimate_headroom_db(profile)

    _, active_path = _active_camilla_config_path()
    active_name = Path(active_path).name if active_path else ""
    active_generated = (
        active_name.startswith("correction_")
        or active_name in {"sound_current.yml", "sound_audition.yml"}
    )
    status = "ok"
    drift = ""
    if profile.enabled and filter_count and not active_generated:
        status = "warn"
        drift = " (saved profile not reflected in active generated config)"

    detail = (
        f"enabled={profile.enabled} curve={profile.curve_id} "
        f"filters={filter_count} headroom={headroom_db:.1f}dB{drift}"
    )
    return CheckResult("sound profile", status, detail)


def check_dsp_apply_state() -> CheckResult:
    from jasper.dsp_apply import last_dsp_apply_state

    state = last_dsp_apply_state()
    if state is None:
        return CheckResult(
            "DSP apply state",
            "ok",
            "no DSP apply attempts recorded yet",
        )

    result = str(state.get("result") or "unknown")
    phase = str(state.get("phase") or "unknown")
    source = str(state.get("source") or "unknown")
    candidate = state.get("candidate_config_path")
    op_id = str(state.get("op_id") or "")[:8]

    if state.get("rollback_attempted") and state.get("rollback_succeeded") is False:
        status = "fail"
    elif result == "success":
        status = "ok"
    else:
        status = "warn"

    detail = f"source={source} result={result} phase={phase} op={op_id}"
    if candidate:
        detail += f" config={candidate}"
    return CheckResult("DSP apply state", status, detail)


def check_correction_latest_bundle() -> CheckResult:
    from jasper.correction import bundles

    sessions_dir = Path(
        os.environ.get(
            "JASPER_CORRECTION_SESSIONS_DIR",
            str(_correction_root() / "sessions"),
        )
    )
    latest = bundles.latest_bundle(sessions_dir)
    if latest is None:
        return CheckResult(
            "latest correction bundle", "ok",
            f"no bundles under {sessions_dir} yet",
        )
    bundle_dir = Path(str(latest["bundle_dir"]))
    issues = bundles.validate_bundle(bundle_dir)
    fail_issues = [i for i in issues if i.severity == "fail"]
    warn_issues = [i for i in issues if i.severity == "warn"]
    summary = (
        f"session={latest.get('session_id')} state={latest.get('state')} "
        f"schema={latest.get('bundle_schema_version')}"
    )
    if fail_issues:
        return CheckResult(
            "latest correction bundle", "fail",
            summary + "; " + "; ".join(i.message for i in fail_issues[:3]),
        )
    if warn_issues:
        return CheckResult(
            "latest correction bundle", "warn",
            summary + "; " + "; ".join(i.message for i in warn_issues[:3]),
        )
    if not latest.get("mic_calibration"):
        return CheckResult(
            "latest correction bundle", "warn",
            summary + "; last completed measurement used no calibrated mic",
        )
    return CheckResult("latest correction bundle", "ok", summary)


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
    sync_checks: list[DoctorCheck] = [
        check_env_file,
        check_speaker_name,
        ("provider key", lambda: check_provider_key(cfg)),
        ("mic ALSA card", lambda: check_mic_card_matches_config(cfg)),
        check_loopback,
        ("mic capture", lambda: check_mic_capture(cfg)),
        ("tts output", lambda: check_tts_open(cfg)),
        ("openWakeWord models", lambda: check_openwakeword_model(cfg)),
        # Per-renderer health: each daemon's own surface.
        ("librespot.service", lambda: check_librespot_running(cfg)),
        check_shairport_sync_ap2,
        check_nqptp_running,
        check_bluealsa,
        check_jasper_mux,
        ("Spotify auth", lambda: check_spotify_cache(cfg)),
        ("Spotify Connect device", lambda: check_spotify_connect_device(cfg)),
        ("Google OAuth", lambda: check_google_tokens(cfg)),
        ("Home Assistant", lambda: check_home_assistant(cfg)),
        # Citi Bike: GBFS reachability + saved-station drift detection.
        # Skip-if-not-configured matches the home_assistant pattern.
        ("Citi Bike", lambda: check_citibike(cfg)),
        check_apple_dongle_audio,
        check_dongle_headphone_at_max,
        ("state dir", lambda: check_state_dir(cfg)),
        # Room-correction observability: socket-activated service
        # health, state-dir drift, currently-loaded correction profile,
        # and the newest replay/debug bundle. These are deliberately
        # lightweight so `jasper-doctor` remains safe to run before a
        # correction session.
        check_correction_web_service,
        check_correction_state_dirs,
        check_camilla_volume_limit,
        check_correction_current_config,
        check_sound_profile,
        check_dsp_apply_state,
        check_correction_latest_bundle,
        check_ram,
        # Stage 1 memory-pressure resilience (docs/HANDOFF-resilience.md
        # "Memory-pressure resilience"). All five checks are drift
        # detectors — they verify Stage 1's protections are actually
        # in effect after install.sh. Each fails soft (warn, not fail)
        # because Stage 1 protections are belt-and-suspenders, not
        # critical-path.
        check_memory_headroom,
        check_zram_size_ratio,
        check_mglru_min_ttl,
        check_sysctl_drift,
        check_oom_score_adj,
        # T5.1 watchdog escalation — verify StartLimitAction=reboot is
        # still configured on the 4 critical daemons. See
        # docs/HANDOFF-tier5-watchdog-liveness.md.
        check_start_limit_action,
        # Stage 2 audio-protection (shipped 2026-05-24 in response to
        # the stress test showing aec-bridge's 42 MB VmSwap caused
        # audible music degradation). Verifies cgroup memory is
        # enabled + audio-path daemons aren't swapping to zram.
        check_cgroup_memory_enabled,
        check_audio_path_no_swap,
        ("daily spend cap", lambda: check_spend_cap(cfg)),
        check_aec_bridge_running,
        # check_aec_output_card retired in PR 2 — see jasper.cli.doctor
        check_aec_bridge_output_health,
        # Mandatory fan-in daemon (docs/HANDOFF-fan-in-daemon.md).
        # The binary check fails hard if cargo build silently failed
        # during install. The service check probes the UDS endpoint,
        # validates canonical lane wiring, and detects a wedged work
        # loop (catches "process alive but not making progress" — the
        # same shape Tier 5.2's SystemSupervisor protects against at
        # the global level).
        check_fanin_binary_installed,
        # Fan-in is the only supported renderer topology. The wiring
        # check catches stale dmix-era /etc/asound.conf after deploy;
        # the service check catches a dead/missing summing daemon.
        check_fanin_asound_wiring,
        check_fanin_service,
        # Final-output owner for the outputd cutover branch.
        check_outputd_service,
        # Reports which additive wake-detection legs the user has
        # armed via the /system Wake detection card (raw + DTLN).
        # Doesn't fail on any combination — pure visibility — so
        # the operator sees their config at a glance.
        check_wake_legs_configured,
        # Triple-stream tertiary leg health. Skips cleanly when
        # JASPER_AEC_DTLN_ENABLED is unset (dual-stream / single-stream
        # configs). Catches silent ONNX-load failures that would
        # otherwise degrade triple-stream to dual-stream invisibly.
        check_aec_bridge_dtln_engine,
        check_xvf_firmware_6ch,
        check_xvf_mixer_state,
        # USB sink (jasper-usbsink) — optional fourth source. The
        # checks are RAM-aware: when the service is off they verify
        # nothing leaked, when on they verify the daemon is healthy
        # and the gadget card is registered.
        check_usbsink_dtoverlay,
        check_usbsink_state,
        check_usbsink_card,
        check_usbsink_active_libcomposite,
        check_usbsink_preempt_port_reachable,
        # WiFi: brcmfmac scan suppression is the most common
        # post-bringup foot-gun — silent except in dmesg, and
        # breaks the /wifi/ wizard's primary function.
        check_wifi_regdom,
        # WiFi profile recovery: the guardian's stash must match the
        # active profile, otherwise a dirty-shutdown filesystem loss
        # of /etc/NetworkManager/system-connections/ would either fail
        # to recreate (no stash) or recreate the wrong network (drift).
        check_wifi_guardian,
        # mDNS publishing chain — three checks ordered most-specific
        # cause first so the operator reads the right failure message:
        #   1. daemon installed and running at all
        #   2. our jasper-control service being advertised
        #   3. hostname collision detection (Avahi's silent suffix-resolve)
        check_avahi_daemon,
        check_avahi_jasper_control,
        check_hostname_avahi_consistency,
        check_dial_heartbeat,
        # Multi-device peering. The mode check verifies the env file
        # is parseable (a typo in JASPER_PEERING resolves to OFF, but
        # the user should know about it). The discovery check counts
        # sibling peers visible via mDNS — informational when off,
        # confirms reachability when on.
        check_peering_mode,
        check_peering_discovery,
        # Catch deployment drift on the shairport-sync.conf alsa block —
        # raw `hw:Loopback` silently breaks AirPlay (the d6c946c bug).
        check_shairport_sync_loopback_plughw,
        # Catch named-PCM visibility failures under each renderer's
        # runtime User=. In fan-in mode, EBUSY on an active private
        # lane is OK; Unknown PCM remains a real asoundrc/deploy
        # failure.
        check_renderer_device_resolvable,
    ]
    results = [_run_doctor_check(c) for c in sync_checks]
    results.append(await _run_async_doctor_check(
        "CamillaDSP websocket",
        lambda: check_camilla_websocket(cfg),
    ))
    return results


def check_shairport_sync_loopback_plughw() -> CheckResult:
    """Verify the deployed shairport-sync.conf uses a multi-writer-safe
    renderer device.

    Canonical: `shairport_substream` — AirPlay's private fan-in lane.
    jasper-fanin reads the capture side and publishes the summed music
    stream to CamillaDSP/AEC. A stale `jasper_renderer_in` value means
    shairport is still pointed at the retired renderer-side dmix path.

    Legacy `plughw:Loopback,0,0` and raw `hw:Loopback,0,0` are both
    stale now. The raw form is additionally broken because it bypasses
    ALSA's plug layer.

    Check runs against the DEPLOYED file (not the repo) so it catches
    both kinds of drift: branch not yet merged, and manual on-Pi edits."""
    label = "shairport-sync.conf: output_device"
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
    if "shairport_substream" in line:
        return CheckResult(
            label, "ok",
            "shairport_substream (fan-in private AirPlay lane)",
        )
    if "jasper_renderer_in" in line:
        return CheckResult(
            label, "fail",
            "jasper_renderer_in — stale retired dmix path. Re-run "
            "deploy/install.sh so shairport renders to shairport_substream.",
        )
    if 'plughw:Loopback' in line:
        return CheckResult(
            label, "warn",
            "plughw:Loopback,0,0 — stale pre-fan-in wiring. Redeploy "
            "to render shairport_substream, AirPlay's private fan-in lane.",
        )
    if '"hw:Loopback' in line or "'hw:Loopback" in line:
        return CheckResult(
            label, "fail",
            "output_device uses raw `hw:Loopback,0,0` — AirPlay sessions "
            "will be silently rejected because Loopback is locked at "
            "48 kHz and shairport requests 44.1 kHz. Symptom: iPhone / "
            "Mac sees the speaker in the picker but can't establish a session. "
            "Fix: redeploy via `bash scripts/deploy-to-pi.sh`. Source "
            "of truth: deploy/shairport-sync.conf.template.",
        )
    return CheckResult(
        label, "warn",
        f"output_device value not recognized: {line!r}",
    )


# Renderer registry: (label_suffix, runtime_user, parse_function).
# parse_function returns the configured device name, or None if not
# discoverable. Centralising the registry here keeps the probe loop
# below uniform across renderers; adding a fourth renderer is one
# entry.
def _read_first_line_matching(path: Path, predicate) -> Optional[str]:
    """Scan a config file for the first line where `predicate(line)`
    returns truthy. Returns the line stripped, or None."""
    try:
        for ln in path.read_text().splitlines():
            if predicate(ln):
                return ln.strip()
    except OSError:
        return None
    return None


def _renderer_device_shairport() -> Optional[str]:
    """shairport-sync: parse /etc/shairport-sync.conf for output_device.
    Format: `output_device = "shairport_substream";` (libconfig syntax)."""
    ln = _read_first_line_matching(
        Path("/etc/shairport-sync.conf"),
        lambda line: (
            line.lstrip().startswith("output_device")
            and "=" in line
            and not line.lstrip().startswith("//")
        ),
    )
    if not ln:
        return None
    # output_device = "DEVNAME"; — pull the quoted string.
    m = re.search(r'"([^"]+)"', ln) or re.search(r"'([^']+)'", ln)
    return m.group(1) if m else None


def _renderer_device_librespot() -> Optional[str]:
    """librespot: parse the ExecStart= line(s) in librespot.service for
    --device. systemd allows ExecStart to span multiple lines via
    backslash continuation."""
    p = Path("/etc/systemd/system/librespot.service")
    try:
        text = p.read_text()
    except OSError:
        return None
    # Collapse line continuations so we can scan the full ExecStart.
    flat = text.replace("\\\n", " ")
    for ln in flat.splitlines():
        s = ln.strip()
        if not s.startswith("ExecStart=") or "--device" not in s:
            continue
        # --device <DEVNAME>  (may be quoted)
        m = re.search(r"--device\s+(?:'([^']+)'|\"([^\"]+)\"|(\S+))", s)
        if m:
            return m.group(1) or m.group(2) or m.group(3)
    return None


def _renderer_device_bluealsa() -> Optional[str]:
    """bluealsa-aplay: parse the drop-in ExecStart= for --pcm=DEVNAME."""
    # The drop-in is mode-0644 readable; doctor runs as root anyway.
    for path in (
        Path("/etc/systemd/system/bluealsa-aplay.service.d/jts-output.conf"),
        Path("/etc/systemd/system/bluealsa-aplay.service.d/override.conf"),
    ):
        try:
            text = path.read_text()
        except OSError:
            continue
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("ExecStart=") and "--pcm=" in s:
                m = re.search(r"--pcm=(\S+)", s)
                if m:
                    return m.group(1)
    return None


def _systemd_user_for(unit: str) -> Optional[str]:
    """Return the User= field of a systemd unit, or None if missing /
    empty (systemd default = root in that case, which the caller
    handles)."""
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "-p", "User", "--value"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    u = r.stdout.strip()
    return u or None


def _resolve_systemd_env_vars(device: str, unit: str) -> str:
    """Expand `${VAR}` references in a device string using the
    systemd unit's resolved environment.

    Most renderer service files now use literal fan-in lane names, but
    this helper remains useful for operator overrides that use systemd
    environment variables. systemd expands those references at daemon
    start time; when the doctor reads the unit file directly it sees
    the literal `${VAR}` string. Passing that to aplay would fail with
    "Unknown PCM ${VAR}" — a false positive.

    We ask systemd for the unit's resolved environment
    (`systemctl show -p Environment`), which already accounts for
    both `Environment=` directives and `EnvironmentFile=` lookups
    (with the leading-`-` "optional file" semantics). Whatever
    value systemd would substitute at ExecStart time is what we
    pass to aplay.

    Returns the original string unchanged if it contains no
    `${VAR}` references or if resolution fails (best-effort — the
    caller's aplay probe will then fail loudly with a clear
    error, which is the right behavior).
    """
    if "${" not in device:
        return device
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "-p", "Environment", "--value"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return device
    if r.returncode != 0:
        return device
    # systemd's `Environment` output is a single line of
    # space-separated KEY=VALUE pairs (after merging Environment=
    # directives and any EnvironmentFile= files). Values that
    # contain spaces are quoted, but ALSA PCM names never do, so
    # naive splitting is safe for our use case.
    env_map: dict[str, str] = {}
    for token in r.stdout.split():
        if "=" in token:
            key, _, value = token.partition("=")
            # Strip surrounding quotes systemd may add.
            env_map[key] = value.strip().strip('"').strip("'")

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        return env_map.get(name, match.group(0))

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, device)


def _probe_open_as_user(device: str, user: Optional[str]) -> tuple[bool, str]:
    """Attempt to open `device` for ~0.1 s of silence playback AS `user`.
    Returns (success, detail). success=True means snd_pcm_open and a
    short write both succeeded; detail is the underlying aplay stderr
    for diagnostics (best-effort short).

    Why aplay + /dev/zero: it exercises the same code path the renderer
    uses (alsalib's snd_pcm_open through the user-space plugin chain)
    while writing only silence — sample-wise additive into any mix,
    so safe to run while music is playing.
    """
    cmd = [
        "timeout", "0.3",
        "aplay", "-q",
        "-D", device,
        "-c", "2", "-r", "48000", "-f", "S16_LE",
        "/dev/zero",
    ]
    if user:
        cmd = ["sudo", "-n", "-u", user, *cmd]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"probe subprocess failed: {e}"
    # Exit code 124 = timeout fired = aplay was happily writing
    # silence for the full 0.3 s, which means open + write succeeded.
    # Exit code 0 = aplay exited cleanly before timeout (rare; means
    # /dev/zero was fully consumed, which won't happen at 0.3 s but
    # still success).
    # Any other code = failure; stderr should explain.
    stderr_tail = (r.stderr or "").strip().splitlines()[-2:]
    detail = " | ".join(stderr_tail)[:200]
    if r.returncode in (0, 124):
        return True, detail
    return False, detail or f"exit={r.returncode}"


_FANIN_PRIVATE_RENDERER_DEVICES = {
    "librespot_substream": 0,
    "shairport_substream": 1,
    "bluealsa_substream": 2,
    "usbsink_substream": 3,
}


def _alsa_busy(detail: str) -> bool:
    return (
        "Device or resource busy" in detail
        or "EBUSY" in detail
        or "errno 16" in detail
    )


def _fanin_lane_busy_owner_matches(device: str, unit: str) -> tuple[bool, str]:
    """Return whether an EBUSY private fan-in lane is owned by `unit`.

    An EBUSY aplay probe proves the PCM name resolved, but it does not
    prove the expected renderer owns the lane. The snd-aloop proc status
    exposes `owner_pid`; systemd cgroups expose the owning unit. Combine
    both so a stale test process cannot make doctor green.
    """
    substream = _FANIN_PRIVATE_RENDERER_DEVICES.get(device)
    if substream is None:
        return False, "not a known fan-in private lane"
    status_path = Path(f"/proc/asound/Loopback/pcm0p/sub{substream}/status")
    try:
        text = status_path.read_text()
    except OSError as e:
        return False, f"could not read {status_path}: {e}"
    m = re.search(r"owner_pid\s*:\s*(\d+)", text)
    if not m:
        return False, f"{status_path} has no owner_pid"
    pid = m.group(1)
    cgroup_path = Path(f"/proc/{pid}/cgroup")
    try:
        cgroup = cgroup_path.read_text()
    except OSError as e:
        return False, f"could not read {cgroup_path}: {e}"
    if f"/{unit}" in cgroup:
        return True, f"busy/owned pid={pid}"
    return False, f"busy but owner pid={pid} cgroup={cgroup.strip()!r}"


def check_renderer_device_resolvable() -> CheckResult:
    """Verify each music renderer can actually open the ALSA device
    it's configured to write to, AS its runtime systemd User=.

    The original bug this catches (PR #223, 2026-05-23): renderer users
    could not read the asoundrc that defined the named ALSA PCMs, so
    snd_pcm_open() returned "Unknown PCM" despite config strings looking
    right. A real open attempt catches that class.

    Fan-in caveat: renderer lanes are intentionally private
    single-writer substreams. If the renderer is already active, a
    second `aplay -D shairport_substream` probe can return EBUSY. We
    accept that only when /proc/asound's owner_pid belongs to the
    expected systemd unit.

    Method: for each known renderer:
      1. Look up its systemd User=.
      2. Parse its config to find the configured ALSA device.
      3. `sudo -u <user> aplay -D <device> /dev/zero` for a short
         duration. Success = device opens and a write goes through.

    Probe is safe to run anytime. It writes only silence. On idle
    fan-in lanes, the open succeeds; on active fan-in lanes, EBUSY is
    accepted as "owned by the renderer."

    Returns:
      ok    — all configured renderers can open their device as their user
      fail  — any renderer can't open its device (this is the bug class)
      warn  — partial info: some renderer's device or user wasn't
              discoverable (likely the renderer isn't installed; treat
              as informational)
    """
    label = "renderer ALSA device resolvable"
    renderers = [
        ("shairport-sync", "shairport-sync.service",
         _renderer_device_shairport),
        ("librespot",      "librespot.service",
         _renderer_device_librespot),
        ("bluealsa-aplay", "bluealsa-aplay.service",
         _renderer_device_bluealsa),
    ]
    failures: list[str] = []
    incomplete: list[str] = []
    successes: list[str] = []
    for name, unit, parse_dev in renderers:
        device = parse_dev()
        if device is None:
            incomplete.append(f"{name}: config not found (not installed?)")
            continue
        # If the parsed device contains a ${VAR} reference, ask systemd
        # what value it would substitute at ExecStart time. Otherwise
        # the aplay probe below will fail with "Unknown PCM ${VAR}" —
        # a false positive, since the running daemon has resolved it.
        resolved_device = _resolve_systemd_env_vars(device, unit)
        user = _systemd_user_for(unit)
        ok, detail = _probe_open_as_user(resolved_device, user)
        who = user or "root"
        # Show both the literal-parsed and resolved values when they
        # differ, so the operator can spot a misconfigured env file
        # without re-reading the unit themselves.
        display = (
            f"{resolved_device}"
            if resolved_device == device
            else f"{resolved_device} (from {device})"
        )
        if ok:
            successes.append(f"{name}({who})→{display}")
        elif (
            resolved_device in _FANIN_PRIVATE_RENDERER_DEVICES
            and _alsa_busy(detail)
        ):
            owned, owner_detail = _fanin_lane_busy_owner_matches(
                resolved_device, unit,
            )
            if owned:
                successes.append(f"{name}({who})→{display} {owner_detail}")
            else:
                failures.append(f"{name}({who})→{display}: {owner_detail}")
        else:
            failures.append(f"{name}({who})→{display}: {detail}")
    if failures:
        return CheckResult(
            label, "fail",
            "; ".join(failures) + ". This is the bug class PR #223 "
            "addressed — verify /etc/asound.conf exists and is mode "
            "0644 so non-root renderer users can resolve user-space "
            "ALSA PCM names. EBUSY is expected only for active fan-in "
            "private lanes; Unknown PCM is always a real failure.",
        )
    if not successes:
        # All renderers were unknown — probably a stripped image.
        return CheckResult(
            label, "warn",
            "; ".join(incomplete) if incomplete
            else "no renderers configured",
        )
    detail = "; ".join(successes)
    if incomplete:
        detail += " (skipped: " + "; ".join(incomplete) + ")"
    return CheckResult(label, "ok", detail)


def check_peering_mode() -> CheckResult:
    """Verify /var/lib/jasper/peering.env is parseable.

    Off by default; the user opts in via the /peers/ web wizard. We
    return `ok` for both OFF (deliberate) and ON (configured) — the
    `warn`/`fail` cases catch broken env files only."""
    label = "peering: mode"
    p = Path("/var/lib/jasper/peering.env")
    if not p.exists():
        return CheckResult(
            label, "ok",
            "off (default) — enable at http://<hostname>/peers/",
        )
    raw = ""
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if line.startswith("JASPER_PEERING="):
                raw = line.split("=", 1)[1].strip().strip("'\"").lower()
                break
    except OSError as e:
        return CheckResult(label, "warn", f"can't read {p}: {e}")
    if raw in ("", "off", "false", "0", "no", "disabled"):
        return CheckResult(label, "ok", "off (configured)")
    if raw in ("on", "true", "1", "yes", "enabled"):
        return CheckResult(
            label, "ok",
            "on — jasper-control runs the peering daemon",
        )
    return CheckResult(
        label, "warn",
        f"unknown JASPER_PEERING={raw!r}; defaults to off. "
        "Edit /var/lib/jasper/peering.env or use the /peers/ wizard.",
    )


def check_peering_discovery() -> CheckResult:
    """Browse `_jasper-peer._udp` to count sibling JTS speakers
    visible on the LAN.

    Informational when peering is OFF (we don't advertise; expected
    to see zero peers). When peering is ON, this is the smoke test
    that mDNS-SD is working — if siblings are reachable, this Pi
    should see them here."""
    label = "peering: discovery"
    bin_path = shutil.which("avahi-browse")
    if bin_path is None:
        return CheckResult(
            label, "warn",
            "avahi-browse missing (apt install avahi-utils) — can't "
            "verify peer discovery.",
        )
    proc = _run([bin_path, "-rt", "_jasper-peer._udp"], timeout=4.0)
    if proc.returncode != 0:
        return CheckResult(
            label, "warn",
            f"avahi-browse exited {proc.returncode}. Is avahi-daemon "
            "running? (`systemctl status avahi-daemon`).",
        )
    # Count distinct peer_id TXT records.
    peer_ids: set[str] = set()
    for line in proc.stdout.splitlines():
        # avahi-browse -r output includes lines like:
        #     txt = ["peer_id=abc-uuid" "room=kitchen" "primary=0" "proto=1"]
        if "peer_id=" in line:
            for token in line.replace('"', " ").split():
                if token.startswith("peer_id="):
                    peer_ids.add(token[len("peer_id="):].strip(",[]"))
    # Drop our own peer_id if we know it (so the count is "siblings").
    local_id = _local_peer_id()
    if local_id:
        peer_ids.discard(local_id)
    if not peer_ids:
        return CheckResult(
            label, "ok",
            "0 sibling peers visible (single-device mode)",
        )
    sample = ", ".join(sorted(peer_ids)[:3])
    return CheckResult(
        label, "ok",
        f"{len(peer_ids)} sibling peer(s) visible: {sample}",
    )


def _local_peer_id() -> str:
    """Read /var/lib/jasper/peer_id (returns '' if missing).

    Best-effort — used by check_peering_discovery to filter ourselves
    out of the visible-peer count. A missing file is fine (peering
    template install never ran), the count is just slightly inflated."""
    try:
        return Path("/var/lib/jasper/peer_id").read_text().strip()
    except OSError:
        return ""


def check_avahi_daemon() -> CheckResult:
    """avahi-daemon is the mDNS *publisher* — without it the speaker
    is invisible to `<hostname>.local` resolution from other devices,
    the dial can't auto-discover via `_jasper-control._tcp`, and any
    user-facing mention of "visit http://jts.local/" silently fails.

    Pi OS Lite Trixie ships `libnss-mdns` (resolution-side) but does
    NOT pre-install or enable avahi-daemon. install.sh added the
    package starting 2026-05-24; on Pis bootstrapped before that this
    check flags the gap so the operator knows to re-run install.sh.

    Fires BEFORE check_avahi_jasper_control so the operator sees the
    package/daemon failure first, not the indirect "service not
    advertised" message.
    """
    label = "avahi-daemon"
    state = _run(["systemctl", "is-active", "avahi-daemon.service"]).stdout.strip()
    if state == "active":
        return CheckResult(label, "ok", "running (mDNS publishing enabled)")
    # is-active prints "inactive" for both unit-not-found and stopped.
    # Distinguish via `status` exit code: 4 means unit not loaded.
    status = _run(["systemctl", "status", "avahi-daemon.service"])
    if "could not be found" in status.stderr.lower() or status.returncode == 4:
        return CheckResult(
            label, "fail",
            "avahi-daemon NOT installed. Re-run deploy/install.sh — "
            "it now installs the package (2026-05-24+). Without it, "
            "`<hostname>.local` doesn't resolve and the dial can't "
            "auto-discover this Pi.",
        )
    return CheckResult(
        label, "fail",
        f"systemctl is-active = '{state}'. "
        "`sudo systemctl enable --now avahi-daemon` to fix.",
    )


def check_hostname_avahi_consistency() -> CheckResult:
    """Detect Avahi's silent hostname suffix-resolve on collision.

    When two devices on the same LAN both claim the same hostname,
    Avahi's conflict-resolution renames the loser to `<hostname>-2`,
    `<hostname>-3`, etc. — the OS-level `/etc/hostname` stays as the
    user configured it, but `avahi-resolve` and outbound mDNS replies
    use the suffixed form. The user has no UI surface that tells
    them this happened; they just notice "my second speaker isn't
    reachable as jts.local — what's going on?".

    Approach: resolve `<sys_hostname>.local` via `avahi-resolve-host-name`
    and compare the result to one of our own interface IPs. If the
    name we configured resolves to someone *else's* IP, another
    device on the LAN won the claim and we got suffix-resolved.
    Decoupled from `_jasper-control._tcp` so it works before
    jasper-control is up.
    """
    label = "hostname ↔ avahi consistency"
    sys_hostname = _run(["hostname", "-s"]).stdout.strip()
    if not sys_hostname:
        return CheckResult(label, "warn", "could not read system hostname")
    bin_path = shutil.which("avahi-resolve-host-name")
    if bin_path is None:
        return CheckResult(
            label, "warn",
            "avahi-resolve-host-name missing (apt install avahi-utils)",
        )
    # -4: IPv4 only. Output is one line: `<hostname>.local <IP>`.
    proc = _run([bin_path, "-4", f"{sys_hostname}.local"], timeout=4.0)
    if proc.returncode != 0:
        # Don't fail — check_avahi_daemon already reports the root
        # cause if the daemon isn't running.
        return CheckResult(
            label, "warn",
            f"avahi-resolve-host-name {sys_hostname}.local exited "
            f"{proc.returncode}. Likely avahi-daemon not yet "
            f"advertising us — check_avahi_daemon reports the cause.",
        )
    parts = proc.stdout.strip().split()
    if len(parts) < 2:
        return CheckResult(
            label, "warn",
            f"unexpected avahi-resolve output: {proc.stdout.strip()!r}",
        )
    resolved_ip = parts[1]
    # `hostname -I` prints space-separated IPs for all up interfaces.
    own_ips = set(_run(["hostname", "-I"]).stdout.split())
    if resolved_ip in own_ips:
        return CheckResult(
            label, "ok",
            f"`{sys_hostname}.local` resolves to us ({resolved_ip})",
        )
    return CheckResult(
        label, "warn",
        f"`{sys_hostname}.local` resolves to {resolved_ip}, but this "
        f"Pi's IPs are {sorted(own_ips)}. Another device on the LAN "
        f"is using your hostname; Avahi suffix-resolved us to "
        f"`{sys_hostname}-N.local`. Pick a unique hostname: "
        f"`sudo hostnamectl set-hostname <new>` then reboot.",
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
        help="Active probe — play a brief sine into correction_substream "
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
    try:
        results = asyncio.run(run_async(cfg))
    except Exception as e:  # noqa: BLE001
        if args.json:
            import json as _json
            detail = _exception_detail(e)
            print(_json.dumps({
                "error": f"doctor crashed: {detail}",
                "fails": 1,
                "warns": 0,
                "results": [{
                    "name": "jasper-doctor",
                    "status": "fail",
                    "detail": detail,
                }],
            }))
            sys.exit(1)
        raise
    if args.json:
        sys.exit(render_json(results))
    sys.exit(render(results))


if __name__ == "__main__":
    main()
