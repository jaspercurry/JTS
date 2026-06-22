# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — audio domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import time
from pathlib import Path
from ...audio_hardware.dac import (
    APPLE_USB_C_DONGLE_ID,
    mixer_control_groups_for as _dac_mixer_control_groups_for,
)
from ...camilla_config_contract import DEFAULT_VOLUME_LIMIT_DB
from ...config import Config
from ...env_load import parse_env_file
from ...output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    OutputHardwareState,
    load_state as _load_output_hardware_state,
)
from ...voice.input_presence import voice_parked_no_mic
from ._registry import doctor_check
from ._shared import (
    CheckResult,
    _active_audio_dac_env,
    _parked_as_bonded_follower,
    _active_audio_dac_id,
    _camilla_block_field,
    _run,
)
from .correction import _active_camilla_config_path


_OBSERVED_OUTPUT_HARDWARE_CLOCK_ISSUE_CODES = frozenset({
    "dual_apple_observation_missing",
    "dual_apple_usb_topology_mismatch",
    "dual_apple_usb_topology_unknown",
    "dual_apple_stable_identity_missing",
    "dual_apple_endpoint_not_synchronous",
})


def _observed_output_hardware_clock_blockers(
    clock: dict[str, object],
) -> list[dict[str, object]]:
    issues = clock.get("issues")
    if not isinstance(issues, list):
        return []
    blockers: list[dict[str, object]] = []
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("severity") != "blocker":
            continue
        code = str(issue.get("code") or "")
        if code.startswith("dual_apple_observed_") or (
            code in _OBSERVED_OUTPUT_HARDWARE_CLOCK_ISSUE_CODES
        ):
            blockers.append(issue)
    return blockers


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

@doctor_check(order=4, group="audio", label="mic ALSA card", needs_cfg=True)
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
    if _parked_as_bonded_follower():
        return CheckResult(
            "mic ALSA card", "ok",
            "parked (bonded follower) — the dumb-follower profile stops "
            "voice + the AEC stack while paired; the leader owns the mic",
        )
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

@doctor_check(order=5, group="audio")
def check_loopback() -> CheckResult:
    proc = _run(["aplay", "-L"])
    if "CARD=Loopback" in proc.stdout:
        return CheckResult("snd-aloop", "ok", "CARD=Loopback present")
    return CheckResult(
        "snd-aloop", "fail",
        "Loopback device missing. `sudo modprobe snd-aloop` or check "
        "/etc/modules-load.d/snd-aloop.conf",
    )

# order=79 stays AFTER resilience's fractional 78.5 insert — the registry
# contract is "the single async check sorts last", not contiguous integers
# (test_doctor_registry). The former order=78 (grouping TTS-separation
# check) was removed 2026-06-11; the gap is intentional.
@doctor_check(order=79, group="audio", label="CamillaDSP websocket", needs_cfg=True, is_async=True)
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

@doctor_check(
    order=6,
    group="audio",
    label="mic capture",
    needs_cfg=True,
    exclusive_group="audio-probe",
)
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
    if _parked_as_bonded_follower():
        return CheckResult(
            "mic capture", "ok",
            "parked (bonded follower) — the dumb-follower profile stops "
            "voice + the AEC stack while paired; the leader owns the mic",
        )
    # Intentionally idle, not broken: the AEC reconciler found no usable
    # mic and parked jasper-voice behind its ConditionPathExists gate.
    # Report ok+expected (mirrors the bonded-follower idiom above) so a
    # mic-less box / a unit mid-unplug isn't a red doctor line. A genuine
    # open failure (marker ABSENT but the device won't open — custom or
    # busy mic) still falls through to the probe + its fail below. See
    # docs/HANDOFF-hotplug-resilience.md "Layer 3".
    if voice_parked_no_mic():
        return CheckResult(
            "mic capture", "ok",
            "no microphone present (expected) — jasper-voice is parked by "
            "the AEC reconciler; plug a mic and it starts automatically",
        )
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

@doctor_check(order=7, group="audio", label="tts output", needs_cfg=True)
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
                "Start jasper-outputd or deploy a pre-outputd rollback tree to return to the "
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

@doctor_check(order=20, group="audio")
def check_output_hardware_state() -> CheckResult:
    """Surface reconciler-owned output hardware state."""

    state = _load_output_hardware_state()
    if state is None:
        return CheckResult(
            "Output hardware state",
            "warn",
            "state file unavailable — run `sudo systemctl start jasper-audio-hardware-reconcile`",
        )
    blocker_codes = [
        str(item.get("code") or "unknown")
        for item in state.issues
        if item.get("severity") == "blocker"
    ]
    detail = (
        f"profile={state.profile_id} status={state.status} "
        f"outputs={state.physical_output_count} apple_dacs={state.apple_dac_count}"
    )
    if blocker_codes or state.status not in {"ready"}:
        return CheckResult(
            "Output hardware state",
            "fail",
            f"{detail} blocked={','.join(blocker_codes) or 'none'}",
        )
    return CheckResult(
        "Output hardware state",
        "ok",
        detail,
    )


@doctor_check(order=20.5, group="audio")
def check_active_speaker_output_hardware_match() -> CheckResult:
    """Keep saved active-speaker topology mismatch out of basic playback health."""

    from jasper.active_speaker.runtime_contract import classify_output_contract
    from jasper.output_topology import (
        OutputTopologyError,
        clock_domain_report,
        load_output_topology_strict,
    )

    try:
        topology = load_output_topology_strict()
    except OutputTopologyError as exc:
        return CheckResult(
            "active speaker output hardware",
            "fail",
            f"saved output topology is unavailable or invalid: {exc}",
        )

    contract = classify_output_contract(topology)
    if not contract.topology_configured:
        return CheckResult(
            "active speaker output hardware",
            "ok",
            "no saved speaker topology configured",
        )

    observed = _load_output_hardware_state()
    if observed is None:
        return CheckResult(
            "active speaker output hardware",
            "warn",
            "current output hardware state unavailable; run `sudo systemctl start jasper-audio-hardware-reconcile`",
        )

    saved = topology.hardware
    saved_count = int(saved.physical_output_count or 0)
    observed_count = int(observed.physical_output_count or 0)
    detail = (
        f"saved={saved.device_id} outputs={saved_count}; "
        f"current={observed.profile_id} status={observed.status} "
        f"outputs={observed_count}"
    )
    clock_blockers: list[dict[str, object]] = []
    if saved.device_id == observed.profile_id and saved_count == observed_count:
        clock_blockers = _observed_output_hardware_clock_blockers(
            clock_domain_report(topology)
        )
    if (
        saved.device_id == observed.profile_id
        and saved_count == observed_count
        and not clock_blockers
    ):
        return CheckResult("active speaker output hardware", "ok", detail)

    status = "fail" if contract.requires_roleful_graph else "warn"
    blocker_detail = ""
    if clock_blockers:
        codes = ",".join(str(issue.get("code") or "") for issue in clock_blockers)
        messages = "; ".join(
            str(issue.get("message") or "") for issue in clock_blockers
            if issue.get("message")
        )
        blocker_detail = (
            f"; current-hardware clock blockers={codes}"
            f"{': ' + messages if messages else ''}"
        )
    suffix = (
        "active speaker actions are blocked; reconnect the saved hardware "
        "or reconfigure the speaker layout"
        if contract.requires_roleful_graph
        else "saved topology differs from currently attached hardware"
    )
    return CheckResult(
        "active speaker output hardware",
        status,
        f"{detail}{blocker_detail}; {suffix}. "
        "Basic output hardware is reported separately.",
    )


def _output_hardware_state_or_none() -> OutputHardwareState | None:
    try:
        return _load_output_hardware_state()
    except Exception:  # noqa: BLE001
        return None


def _effective_output_dac_id(state: OutputHardwareState | None = None) -> str:
    if state is not None and state.profile_id not in {"", "unknown"}:
        return state.profile_id
    return _active_audio_dac_id()


def _apple_output_profile_active(profile_id: str) -> bool:
    return profile_id in {
        APPLE_USB_C_DONGLE_DEVICE_ID,
        DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    }


def _apple_dongle_cards_from_state(
    state: OutputHardwareState | None,
) -> list[str]:
    if state is None:
        return []
    return [
        child.card_id for child in state.child_devices
        if child.device_id == APPLE_USB_C_DONGLE_DEVICE_ID and child.card_id
    ]


@doctor_check(order=21, group="audio")
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
    state = _output_hardware_state_or_none()
    dac_id = _effective_output_dac_id(state)
    if not _apple_output_profile_active(dac_id):
        return CheckResult(
            "Apple dongle", "ok",
            f"skipped — active output DAC is {dac_id}",
        )

    p = _run(["lsusb"])
    usb_count = len(re.findall(r"05ac:110a", p.stdout, re.IGNORECASE))
    expected_count = 2 if dac_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID else 1
    if usb_count < expected_count:
        return CheckResult(
            "Apple dongle", "fail",
            f"expected {expected_count} Apple USB-C adapter(s), "
            f"but lsusb shows {usb_count}",
        )
    cards = _apple_dongle_cards_from_state(state)
    if len(cards) >= expected_count:
        return CheckResult(
            "Apple dongle",
            "ok",
            f"USB + audio interfaces present ({','.join(cards)})",
        )
    if state is not None:
        return CheckResult(
            "Apple dongle",
            "warn",
            f"USB present but only {len(cards)} Apple audio card(s) enumerated; "
            "check analog loads on the 3.5mm jack(s).",
        )
    p = _run(["aplay", "-l"])
    audio_count = len(
        re.findall(
            r"(?:USB Audio.*USB Audio|Apple USB-C to 3\.5mm|Apple.*USB)",
            p.stdout,
            re.IGNORECASE,
        )
    )
    if audio_count >= expected_count:
        return CheckResult("Apple dongle", "ok", "USB + audio interfaces present")
    return CheckResult(
        "Apple dongle", "warn",
        "USB present but audio interfaces not enumerated. "
        "Plug speakers/headphones into the dongle's 3.5mm jack — "
        "the chip stays in low-power mode without an analog load.",
    )

@doctor_check(order=22, group="audio", exclusive_group="audio-probe")
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
    state = _output_hardware_state_or_none()
    dac = _active_audio_dac_env()
    dac_id = _effective_output_dac_id(state)
    control_groups = _dac_mixer_control_groups_for(APPLE_USB_C_DONGLE_ID)
    if not _apple_output_profile_active(dac_id) or not control_groups:
        return CheckResult(
            "Dongle headphone gain", "ok",
            f"skipped — active output DAC is {dac_id}",
        )
    control = next(
        (
            item for item in control_groups[0]
            if item.name == "Headphone" and item.target_percent is not None
        ),
        None,
    )
    if control is None:
        return CheckResult(
            "Dongle headphone gain",
            "ok",
            f"skipped — active output DAC profile {dac_id} has no Headphone target",
        )

    target_pct = int(control.target_percent or 100)
    cards = _apple_dongle_cards_from_state(state) or [dac["card"]]
    low_cards: list[str] = []
    for card_id in cards:
        p = _run(["amixer", "-c", card_id, "sget", control.name])
        if p.returncode != 0:
            return CheckResult(
                "Dongle headphone gain", "fail",
                f"amixer -c {card_id} sget {control.name} failed — dongle not "
                f"enumerated as card {card_id!r}?",
            )
        # amixer prints "Front Left: Playback NN [PP%] [-DD.DDdB] [on]";
        # we want PP. If both channels are present, expect them equal.
        pcts = re.findall(r"\[(\d+)%\]", p.stdout)
        if not pcts:
            return CheckResult(
                "Dongle headphone gain", "warn",
                f"Could not parse percent from amixer output for {card_id} "
                "(format change?).",
            )
        pct = int(pcts[0])
        if pct < target_pct:
            low_cards.append(f"{card_id}:{pct}%")
    if low_cards:
        return CheckResult(
            "Dongle headphone gain", "warn",
            f"Headphone control below {target_pct}% ({', '.join(low_cards)}). "
            "Run `sudo systemctl start jasper-dac-init` to pin at 100%.",
        )
    return CheckResult(
        "Dongle headphone gain", "ok",
        f"Headphone at {target_pct}% on {len(cards)} Apple card(s) "
        "(analog ceiling open)",
    )

@doctor_check(order=49, group="audio")
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

_OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM = "outputd_active_content_capture"

_OUTPUTD_EXPECTED_DAC_PCM = "outputd_dac"

_OUTPUTD_EXPECTED_DUAL_DAC_PCM = "dual_apple_usb_c_dac_4ch"

_OUTPUTD_ENV_PATH = "/var/lib/jasper/outputd.env"

_OUTPUTD_STATUS_SOCKET = "/run/jasper-outputd/control.sock"


def _outputd_reconciled_env() -> dict[str, str]:
    return parse_env_file(
        os.environ.get("JASPER_OUTPUTD_ENV_FILE") or _OUTPUTD_ENV_PATH
    )


def _outputd_active_channels_from_env(env: dict[str, str]) -> int | None:
    raw = str(env.get("JASPER_OUTPUTD_ACTIVE_CHANNELS") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 2 <= value <= 8 else None

@doctor_check(order=50, group="audio")
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

@doctor_check(order=51, group="audio")
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
                except Exception:  # noqa: BLE001
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
    tts = data.get("tts", {})
    if not isinstance(tts, dict) or not bool(tts.get("enabled", False)):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but pre-DSP TTS socket is not enabled. Current "
            "production topology requires TTS/cues to enter jasper-fanin "
            "before CamillaDSP.",
        )

    tts_detail = "tts_enabled=true"
    loudness = tts.get("assistant_loudness")
    if not isinstance(loudness, dict):
        return CheckResult(
            "jasper-fanin service",
            "warn",
            "active with pre-DSP TTS enabled but STATUS is missing "
            "tts.assistant_loudness telemetry; deploy current jasper-fanin "
            "before evaluating TTS loudness.",
        )
    decision_seen = bool(loudness.get("decision_seen", False))
    calibrated = bool(loudness.get("calibrated", False))
    final_gain = loudness.get("final_gain_db")
    if decision_seen and not isinstance(final_gain, (int, float)):
        return CheckResult(
            "jasper-fanin service",
            "warn",
            "active with pre-DSP TTS enabled but "
            "tts.assistant_loudness.decision_seen=true without "
            "numeric final_gain_db.",
        )
    if isinstance(final_gain, (int, float)) and not -60.0 <= float(final_gain) <= 0.0:
        return CheckResult(
            "jasper-fanin service",
            "warn",
            f"active with pre-DSP TTS enabled but "
            f"tts.assistant_loudness.final_gain_db={final_gain!r}; "
            "expected clamped [-60, 0] dB.",
        )
    tts_detail = (
        f"tts_enabled=true, "
        f"tts_pending_frames={tts.get('pending_frames', 0)}, "
        f"assistant_loudness_decision={decision_seen}, "
        f"assistant_loudness_calibrated={calibrated}, "
        f"assistant_final_gain_db={final_gain}"
    )
    return CheckResult(
        "jasper-fanin service",
        "ok",
        f"active, frames_written={frames}, "
        f"input_buffer_frames={input_buffer_frames}, "
        f"output_buffer_frames={output_buffer_frames}, "
        f"output xruns={xruns}, input xruns={','.join(input_xruns) or '0'}, "
        f"progress_age_ms={progress_age}, "
        f"{tts_detail}",
    )

@doctor_check(order=51.5, group="audio")
def check_fanin_tts_drops() -> CheckResult:
    """Dropped TTS audio at fan-in's pending budget means garbled replies.

    fan-in's TTS lane drops whole audio commands that arrive while its
    bounded pending queue is full (it cannot block the socket reader
    without stalling barge-in FLUSH behind queued audio). The Python
    writer paces itself to stay under that budget
    (`_OUTPUTD_PACE_AHEAD_SEC` in jasper/audio_io.py), so a nonzero drop
    counter means assistant/cue audio audibly skipped ("fast-forward"
    garble) since fan-in last started — either the writer-side pacing
    contract regressed or an unpaced client wrote to the TTS socket.
    The 2026-06-11 JTS3 incident surfaced exactly this signature.

    Returns:
      - ok when counters are zero, the TTS lane is disabled, or STATUS
        is unreachable (reachability is owned by 'jasper-fanin service').
      - warn when dropped audio commands/frames > 0 since fan-in start.
    """
    name = "fan-in TTS drops"
    socket_path = "/run/jasper-fanin/control.sock"
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(2.0)
            sock.connect(socket_path)
            sock.sendall(b"STATUS\n")
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        data = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as e:
        return CheckResult(
            name,
            "ok",
            f"not probed ({type(e).__name__}); fan-in reachability is "
            "covered by the 'jasper-fanin service' check",
        )

    tts = data.get("tts")
    if not isinstance(tts, dict) or not tts.get("enabled"):
        return CheckResult(name, "ok", "TTS lane disabled in this topology")

    dropped_frames = int(tts.get("dropped_audio_frames") or 0)
    dropped_commands = int(tts.get("dropped_commands") or 0)
    if dropped_frames == 0 and dropped_commands == 0:
        return CheckResult(
            name,
            "ok",
            f"none since fan-in start (pending_frames="
            f"{tts.get('pending_frames')}, budget_frames="
            f"{tts.get('budget_frames')})",
        )

    sample_rate = int(data.get("output", {}).get("sample_rate") or 48_000)
    dropped_sec = dropped_frames / float(sample_rate)
    return CheckResult(
        name,
        "warn",
        f"{dropped_commands} audio command(s) / ~{dropped_sec:.1f}s of "
        "TTS audio dropped at the pending budget since fan-in start — "
        "assistant replies were audibly garbled/fast-forwarded. Check "
        "`journalctl -u jasper-fanin | grep tts_command_dropped` and the "
        "voice daemon's `paced` turn accounting; an unpaced writer or a "
        "pacing regression is the usual cause.",
    )


@doctor_check(order=52, group="audio")
def check_outputd_service() -> CheckResult:
    """Validate the outputd final-output-owner daemon.

    Current main expects outputd to own the physical DAC. Treat
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
            "for the outputd mainline topology.",
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
    sink_mode = data.get("sink_mode") or "single_alsa"
    outputd_env = _outputd_reconciled_env()
    active_channels = _outputd_active_channels_from_env(outputd_env)
    active_single_alsa = sink_mode == "single_alsa" and active_channels is not None
    expected_content_pcm = (
        _OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM
        if sink_mode == "dual_apple" or active_single_alsa
        else _OUTPUTD_EXPECTED_CONTENT_PCM
    )
    expected_dac_pcm = (
        _OUTPUTD_EXPECTED_DUAL_DAC_PCM
        if sink_mode == "dual_apple"
        else _OUTPUTD_EXPECTED_DAC_PCM
    )
    content = data.get("content", {})
    dac = data.get("dac", {})
    if content.get("pcm") != expected_content_pcm:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"content.pcm={content.get('pcm')!r}; expected "
            f"{expected_content_pcm!r} for sink_mode={sink_mode!r}, "
            f"active_channels={active_channels!r}",
        )
    if dac.get("pcm") != expected_dac_pcm:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"dac.pcm={dac.get('pcm')!r}; expected {expected_dac_pcm!r} "
            f"for sink_mode={sink_mode!r}, active_channels={active_channels!r}",
        )
    reference_outputs = data.get("reference_outputs")
    if not isinstance(reference_outputs, dict):
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS missing reference_outputs speaker-reference contract",
        )
    speaker_reference_source = reference_outputs.get("speaker_reference_source")
    if speaker_reference_source != "outputd_final_electrical":
        return CheckResult(
            "jasper-outputd",
            "fail",
            "reference_outputs.speaker_reference_source="
            f"{speaker_reference_source!r}; expected 'outputd_final_electrical'",
        )
    reference_detail = (
        "speaker_reference_source=outputd_final_electrical, "
        "speaker_reference_active="
        f"{bool(reference_outputs.get('speaker_reference_active', False))}, "
        "speaker_reference_channels="
        f"{reference_outputs.get('speaker_reference_channels')}, "
        f"speaker_reference_udp={reference_outputs.get('udp_target')!r}, "
        f"chip_ref_pcm={reference_outputs.get('chip_ref_pcm')!r}"
    )
    dual_detail = ""
    dual_warning: str | None = None
    active_detail = (
        f", active_channels={active_channels}"
        if active_single_alsa else ""
    )
    if sink_mode == "dual_apple":
        dual = data.get("dual_apple")
        if not isinstance(dual, dict):
            return CheckResult(
                "jasper-outputd",
                "fail",
                "STATUS missing dual_apple runtime health for dual sink",
            )
        dual_a_pcm = dual.get("dac_a_pcm")
        dual_b_pcm = dual.get("dac_b_pcm")
        if not isinstance(dual_a_pcm, str) or not dual_a_pcm:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "dual_apple.dac_a_pcm is missing",
            )
        if not isinstance(dual_b_pcm, str) or not dual_b_pcm:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "dual_apple.dac_b_pcm is missing",
            )
        if dual_a_pcm == dual_b_pcm:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "dual_apple DAC A/B PCMs are identical",
            )
        dual_linked = bool(dual.get("linked", False))
        delay_delta = dual.get("delay_delta_frames")
        delay_error = dual.get("delay_delta_error_frames")
        max_delay = dual.get("max_delay_delta_frames")
        if (
            isinstance(delay_error, int)
            and isinstance(max_delay, int)
            and delay_error > max_delay
        ):
            return CheckResult(
                "jasper-outputd",
                "fail",
                "dual_apple delay delta exceeds runtime budget: "
                f"error={delay_error} max={max_delay}",
            )
        if not dual_linked:
            dual_warning = "dual Apple PCMs are not ALSA-linked"
        dual_detail = (
            f", dual_a_pcm={dual_a_pcm}, dual_b_pcm={dual_b_pcm}, "
            f"dual_linked={dual_linked}, "
            f"dual_delay_delta_frames={delay_delta}, "
            f"dual_delay_delta_error_frames={delay_error}, "
            f"dual_max_delay_delta_frames={max_delay}"
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
    bridge = data.get("content_bridge")
    bridge_detail = "content_bridge=missing"
    bridge_warning: str | None = None
    if isinstance(bridge, dict):
        bridge_enabled = bool(bridge.get("enabled", False))
        bridge_locked = bool(bridge.get("locked", False))
        bridge_fill = int(bridge.get("fill_frames", 0) or 0)
        bridge_target = int(bridge.get("target_fill_frames", 0) or 0)
        bridge_ratio = bridge.get("ratio_ppm")
        bridge_silence = int(bridge.get("silence_frames", 0) or 0)
        bridge_underrun = int(bridge.get("underrun_frames", 0) or 0)
        bridge_overrun = int(bridge.get("overrun_frames", 0) or 0)
        bridge_resync = int(bridge.get("resync_count", 0) or 0)
        bridge_reset = int(bridge.get("reset_count", 0) or 0)
        bridge_clamp = int(bridge.get("ratio_clamp_count", 0) or 0)
        if bridge_enabled:
            bridge_detail = (
                f"content_bridge=rate_match, bridge_locked={bridge_locked}, "
                f"bridge_fill_frames={bridge_fill}, "
                f"bridge_target_fill_frames={bridge_target}, "
                f"bridge_ratio_ppm={bridge_ratio}, "
                f"bridge_silence_frames={bridge_silence}, "
                f"bridge_underrun_frames={bridge_underrun}, "
                f"bridge_overrun_frames={bridge_overrun}, "
                f"bridge_resync_count={bridge_resync}, "
                f"bridge_reset_count={bridge_reset}, "
                f"bridge_ratio_clamp_count={bridge_clamp}"
            )
            anomalies = []
            if bridge_underrun:
                anomalies.append(f"underrun_frames={bridge_underrun}")
            if bridge_overrun:
                anomalies.append(f"overrun_frames={bridge_overrun}")
            if bridge_resync:
                anomalies.append(f"resync_count={bridge_resync}")
            if bridge_reset:
                anomalies.append(f"reset_count={bridge_reset}")
            if bridge_clamp:
                anomalies.append(f"ratio_clamp_count={bridge_clamp}")
            if anomalies:
                bridge_warning = ", ".join(anomalies)
        else:
            bridge_detail = "content_bridge=direct"
    tts = data.get("tts", {})
    tts_pending = int(tts.get("pending_frames", 0) or 0)
    tts_over_budget = bool(tts.get("over_budget", False))
    tts_over_budget_ms = int(tts.get("over_budget_ms", 0) or 0)
    tts_over_budget_streak_ms = int(
        tts.get("over_budget_streak_ms", 0) or 0
    )
    tts_max_pending = int(tts.get("max_pending_frames", 0) or 0)
    tts_dropped_commands = int(tts.get("dropped_commands", 0) or 0)
    tts_dropped_audio_frames = int(
        tts.get("dropped_audio_frames", 0) or 0
    )
    loudness = data.get("assistant_loudness")
    loudness_detail = "assistant_loudness=fan-in-owned"
    if isinstance(loudness, dict):
        decision_seen = bool(loudness.get("decision_seen", False))
        calibrated = bool(loudness.get("calibrated", False))
        final_gain = loudness.get("final_gain_db")
        content_anchor = loudness.get("content_anchor_lufs")
        if decision_seen and not isinstance(final_gain, (int, float)):
            return CheckResult(
                "jasper-outputd",
                "warn",
                "active but assistant_loudness.decision_seen=true without "
                "numeric final_gain_db.",
            )
        if (
            isinstance(final_gain, (int, float))
            and not -60.0 <= float(final_gain) <= 0.0
        ):
            return CheckResult(
                "jasper-outputd",
                "warn",
                f"active but assistant_loudness.final_gain_db={final_gain!r}; "
                "expected clamped [-60, 0] dB.",
            )
        loudness_detail = (
            f"assistant_loudness_decision={decision_seen}, "
            f"assistant_loudness_calibrated={calibrated}, "
            f"assistant_final_gain_db={final_gain}, "
            f"content_anchor_lufs={content_anchor}"
        )
    if progress_age > 1000:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but last_progress_age_ms={progress_age} "
            "(work loop may be wedged; watchdog should fire soon)",
        )
    if dual_warning is not None:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but {dual_warning}. {dual_detail.lstrip(', ')}",
        )
    if bridge_warning is not None:
        return CheckResult(
            "jasper-outputd",
            "warn",
            "active but rate-match content bridge reported anomalies: "
            f"{bridge_warning}. {bridge_detail}",
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
        f"tts_dropped_commands={tts_dropped_commands}, "
        f"tts_dropped_audio_frames={tts_dropped_audio_frames}, "
        f"{bridge_detail}, "
        f"{reference_detail}, "
        f"{loudness_detail}, "
        f"progress_age_ms={progress_age}"
        f"{active_detail}"
        f"{dual_detail}",
    )

@doctor_check(order=52.6, group="audio")
def check_aec_clock_drift() -> CheckResult:
    """Surface the passive chip-AEC clock-drift estimate (Layer 0).

    Reads ``reference_outputs.aec_clock`` from outputd STATUS — the
    observe-only SRO (sample-rate-offset) estimator's verdict, ppm, and
    latency budget. This is purely diagnostic; no audio path depends on it.

      - skip (ok + "skipped — …") when outputd is disabled/inactive,
        STATUS is unreachable or invalid, the chip reference is not
        configured, or the aec_clock block is absent (pre-Layer-0 builds /
        no XVF reference). Mirrors the skip-if-not-configured idiom used by
        the other audio checks — a non-applicable probe is OK, not a fail.
      - warn only when sro_estimator_status == "untrusted" (the clock
        signal itself cannot be trusted right now).
      - ok otherwise: coherent, compensable (a real steady offset a future
        layer would compensate — the *expected* state on independent-clock
        DACs like the HiFiBerry), and observing (still measuring) are all
        healthy. Echoes the estimate and the latency budget.
    """
    label = "AEC clock drift"
    enabled = _run(
        ["systemctl", "is-enabled", "jasper-outputd.service"]
    ).stdout.strip()
    if enabled in {"not-found", "disabled", ""}:
        return CheckResult(label, "ok", "skipped — jasper-outputd not enabled")
    active = _run(
        ["systemctl", "is-active", "jasper-outputd.service"]
    ).stdout.strip()
    if active != "active":
        return CheckResult(label, "ok", "skipped — jasper-outputd not active")

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
        return CheckResult(label, "ok", f"skipped — STATUS unreachable: {e}")
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    body = b"".join(chunks).decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return CheckResult(label, "ok", "skipped — STATUS returned invalid JSON")

    reference_outputs = data.get("reference_outputs")
    if not isinstance(reference_outputs, dict):
        return CheckResult(label, "ok", "skipped — STATUS missing reference_outputs")
    if reference_outputs.get("chip_ref_pcm") is None:
        return CheckResult(label, "ok", "skipped — chip reference not configured")
    aec_clock = reference_outputs.get("aec_clock")
    if not isinstance(aec_clock, dict):
        return CheckResult(
            label, "ok", "skipped — outputd build predates aec_clock observation"
        )

    verdict = aec_clock.get("verdict")
    status = aec_clock.get("sro_estimator_status")
    sro_ppm = aec_clock.get("chip_ref_sro_ppm")
    reason = aec_clock.get("verdict_reason")
    # Observe mode: the chip-ref writer was armed purely to MEASURE drift on
    # the software-AEC3 mic path (chip-AEC observe mode), not for production
    # chip-AEC. Surfaced so an operator can tell why the writer is running.
    observe = aec_clock.get("observe")
    latency = aec_clock.get("latency") or {}
    dac_ms = latency.get("dac_presentation_ms")
    playback_ms = latency.get("playback_queue_ms")
    chip_ref_ms = latency.get("chip_ref_queue_ms")
    detail = (
        f"verdict={verdict}, sro_estimator_status={status}, "
        f"observe={observe}, chip_ref_sro_ppm={sro_ppm}, "
        f"dac_presentation_ms={dac_ms}, playback_queue_ms={playback_ms}, "
        f"chip_ref_queue_ms={chip_ref_ms}"
    )
    # Warn only on a genuinely untrusted clock signal. "observing" (still
    # measuring at startup) and "compensable" (real drift a future layer
    # handles — expected on independent-clock DACs) are both healthy; warning
    # on them would cry wolf at boot and on exactly the DACs we target.
    if status == "untrusted":
        return CheckResult(
            label,
            "warn",
            f"chip-AEC clock drift cannot be trusted: {reason}. {detail}",
        )
    return CheckResult(label, "ok", detail)


def _devices_volume_limit_from_text(text: str) -> float | None:
    """``devices.volume_limit`` from a CamillaDSP config, or None if absent /
    null. Raises ValueError on a non-numeric value (the caller surfaces it as a
    fail). Reads via the shared :func:`_camilla_block_field` scanner."""
    value = _camilla_block_field(text, "devices", "volume_limit")
    if value is None or value in {"", "null", "~"}:
        return None
    return float(value)

@doctor_check(order=28, group="audio")
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

@doctor_check(order=28.5, group="audio")
def check_active_speaker_runtime_graph() -> CheckResult:
    """Fail closed if a roleful/protected topology is running flat stereo."""
    from jasper.active_speaker.runtime_contract import (
        classify_camilla_graph,
        classify_output_contract,
    )
    from jasper.active_speaker.staging import load_staged_startup_config
    from jasper.output_topology import OutputTopologyError, load_output_topology_strict

    try:
        topology = load_output_topology_strict()
    except OutputTopologyError as exc:
        return CheckResult(
            "active speaker runtime graph",
            "fail",
            f"saved output topology is unavailable or invalid: {exc}",
        )
    contract = classify_output_contract(topology)
    if not contract.requires_roleful_graph:
        return CheckResult(
            "active speaker runtime graph",
            "ok",
            f"{contract.classification}: no roleful/protected outputs configured",
        )

    statefile, config_path = _active_camilla_config_path()
    if config_path is None:
        return CheckResult(
            "active speaker runtime graph",
            "fail",
            (
                f"could not read config_path from {statefile}; saved topology "
                "has roleful/protected outputs"
            ),
        )
    path = Path(config_path)
    if not path.exists():
        return CheckResult(
            "active speaker runtime graph",
            "fail",
            f"statefile points at missing config {config_path}",
        )
    graph = classify_camilla_graph(
        path,
        topology,
        staged_config=load_staged_startup_config(),
    )
    if graph.allowed:
        return CheckResult(
            "active speaker runtime graph",
            "ok",
            f"{graph.classification} is legal for {contract.classification}",
        )

    detail = (
        graph.issues[0]["message"]
        if graph.issues
        else "Camilla graph is unsafe for saved active speaker topology"
    )
    return CheckResult("active speaker runtime graph", "fail", detail)

def _sound_profile_path() -> Path:
    return Path(
        os.environ.get(
            "JASPER_SOUND_PROFILE_PATH",
            "/var/lib/jasper/sound_profile.json",
        )
    )

@doctor_check(order=30, group="audio")
def check_sound_profile() -> CheckResult:
    from jasper.sound.profile import (
        SoundProfile,
        build_sound_filters,
        estimate_headroom_db,
    )
    from jasper.sound.settings import load_sound_settings, output_trim_db

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
    settings = load_sound_settings()
    trim = output_trim_db(profile, settings)

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
        f"filters={filter_count} headroom={headroom_db:.1f}dB "
        f"match_loudness={'on' if settings.match_loudness else 'off'} "
        f"output_trim={trim:.1f}dB{drift}"
    )
    return CheckResult("sound profile", status, detail)

@doctor_check(order=31, group="audio")
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
