# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — audio domain."""
from __future__ import annotations

import os
import re
import shutil
import socket
from pathlib import Path
from ...audio_hardware.dac import (
    MixerControl,
    by_id as _dac_profile_for,
)
from ...active_speaker.environment import camilla_statefile_path
from ...camilla import CamillaController, CamillaUnavailable
from ...camilla_config_contract import (
    DEFAULT_VOLUME_LIMIT_DB,
    parse_camilla_devices_config,
    resolve_camilla_chunksize,
)
from ...config import Config
from ...fanin_coupling import RING_PCM_DEVICES, ring_capacity_frames
from ... import ring_assets
from ...mics import xvf3800
from ...output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    OutputHardwareState,
    mixer_index_for_db as _mixer_index_for_db,
    mixer_pins_for_state as _mixer_pins_for_state,
)
from ...mic_presence import MicPresence
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import (
    REASON_TOPOLOGY_UNREADABLE,
    CheckResult,
    _group_writable_dir,
    _parked_follower_result,
    _run,
)
from .correction import (
    REASON_CAMILLA_CONFIG_MISSING,
    REASON_CAMILLA_CONFIG_UNREADABLE,
    REASON_CAMILLA_STATEFILE_UNREADABLE,
)

# Closed vocabulary for this module's `CheckResult.reason`: one snake_case
# constant per distinct outcome branch below. Every `warn`/`fail` carries one;
# an `ok` carries one only where the ok itself is a fact a consumer branches on
# (not-applicable, skipped, an informational sub-state). `detail` stays the
# human sentence and is free to reword; tests pin `status` and `reason`
# (ADR-0233 rule 3).


REASON_ALSA_TOOL_MISSING = "alsa_tool_missing"
REASON_ALSA_CARD_ABSENT = "alsa_card_absent"

REASON_MIC_ABSENT = "mic_absent"
REASON_MIC_ABSENT_DEFERRED = "mic_absent_deferred"
REASON_MIC_UDP_TRANSPORT = "mic_udp_transport"
REASON_MIC_DEVICE_UNNAMED = "mic_device_unnamed"
REASON_MIC_DEVICE_MALFORMED = "mic_device_malformed"
REASON_MIC_CARD_ABSENT = "mic_card_absent"
REASON_MIC_CAPTURE_SILENT = "mic_capture_silent"
REASON_MIC_CAPTURE_LOW_SIGNAL = "mic_capture_low_signal"
REASON_MIC_CAPTURE_OPEN_FAILED = "mic_capture_open_failed"
REASON_MIC_HELD_BY_VOICE = "mic_held_by_voice"

REASON_LOOPBACK_MISSING = "loopback_missing"

REASON_CAMILLA_UNREACHABLE = "camilla_unreachable"
REASON_CAMILLA_VOLUME_ABOVE_CEILING = "camilla_volume_above_ceiling"

REASON_TTS_OUTPUTD_UNREACHABLE = "tts_outputd_unreachable"

REASON_OUTPUT_HARDWARE_STATE_UNAVAILABLE = "output_hardware_state_unavailable"
REASON_OUTPUT_HARDWARE_BLOCKED = "output_hardware_blocked"
REASON_OUTPUT_HARDWARE_MISMATCH = "output_hardware_mismatch"
REASON_OUTPUT_HARDWARE_CLOCK_BLOCKED = "output_hardware_clock_blocked"
REASON_OUTPUT_HARDWARE_DEGRADED = "output_hardware_degraded"

REASON_TOPOLOGY_NOT_CONFIGURED = "output_topology_not_configured"

REASON_CAMILLA_CONFIG_DIR_MISSING = "camilla_config_dir_missing"
REASON_CAMILLA_CONFIG_DIR_UNREADABLE = "camilla_config_dir_unreadable"
REASON_CAMILLA_CONFIG_DIR_NOT_WRITABLE = "camilla_config_dir_not_writable"

REASON_DAC_SYNC_NOT_APPLICABLE = "dac_sync_not_applicable"
REASON_DAC_SYNC_NO_PLAYBACK_CARDS = "dac_sync_no_playback_cards"
REASON_DAC_SYNC_I2S = "dac_sync_i2s"
REASON_DAC_SYNC_TAG_ABSENT = "dac_sync_tag_absent"
REASON_DAC_SYNC_ASYNC = "dac_sync_async"

REASON_APPLE_DONGLE_NOT_APPLICABLE = "apple_dongle_not_applicable"
REASON_APPLE_DONGLE_ABSENT = "apple_dongle_absent"
REASON_APPLE_DONGLE_NO_AUDIO_CARD = "apple_dongle_no_audio_card"
REASON_APPLE_DONGLE_USB_MISSING = "apple_dongle_usb_missing"
REASON_APPLE_DONGLE_CARDS_MISSING = "apple_dongle_cards_missing"

REASON_DAC_MIXER_PINS_NOT_APPLICABLE = "dac_mixer_pins_not_applicable"
REASON_DAC_MIXER_PINS_NOT_HELD = "dac_mixer_pins_not_held"

REASON_VOLUME_LIMIT_INVALID = "volume_limit_invalid"
REASON_VOLUME_LIMIT_ABSENT = "volume_limit_absent"
REASON_VOLUME_LIMIT_ABOVE_CEILING = "volume_limit_above_ceiling"

REASON_RING_CHUNK_NOT_APPLICABLE = "ring_chunk_not_applicable"
REASON_RING_CHUNK_CLAMPED = "ring_chunk_clamped"
REASON_RING_TARGET_LEVEL_ABOVE_CEILING = "ring_target_level_above_ceiling"
REASON_RING_CHUNK_ABOVE_CAPACITY = "ring_chunk_above_capacity"


_OBSERVED_OUTPUT_HARDWARE_CLOCK_ISSUE_CODES = frozenset({
    "dual_apple_observation_missing",
    "dual_apple_usb_topology_mismatch",
    "dual_apple_usb_topology_unknown",
    "dual_apple_stable_identity_missing",
    "dual_apple_endpoint_not_synchronous",
})

# Re-exported from the shared jasper.ring_assets SSOT (which owns the paths and
# their constraints) so the doctor probe and the coupling reconciler's
# activation gate name the same files.
_JTS_RING_ALSA_PLUGIN_DIR = ring_assets.RING_ALSA_PLUGIN_DIR
_JTS_RING_IOPLUG_SO = ring_assets.RING_IOPLUG_SO
_JTS_RING_CONF_D = ring_assets.RING_CONF_D
_JTS_RING_SHM_DIR = ring_assets.RING_SHM_DIR


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
        return CheckResult(
            label, "fail", f"{kind} not in PATH", reason=REASON_ALSA_TOOL_MISSING,
        )
    proc = _run([bin_path, "-L"])
    if name in proc.stdout:
        return CheckResult(label, "ok", f"CARD={name}")
    return CheckResult(
        label, "fail",
        f"no ALSA device with CARD={name} found in `{kind} -L`. "
        f"Plug in the device or fix the configured name.",
        reason=REASON_ALSA_CARD_ABSENT,
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

def _soften_for_push_to_talk(
    result: CheckResult, presence: MicPresence,
) -> CheckResult:
    """Downgrade a *local* mic failure to an advisory when an accessory is paired.

    A speaker with no local microphone but a paired mic-bearing remote has its
    voice-input gate legitimately open (issue #2205), so a red failure is the
    wrong register. The local finding stays visible and only the register
    changes; a ``warn``/``ok`` result is returned untouched. Applied to
    device-absent / cannot-open failures only — a mic that opens but records
    silence is present-and-broken, and stays red."""
    if result.status != "fail" or not presence.accessory_present:
        return result
    return CheckResult(
        result.name,
        "warn",
        f"no local microphone; {presence.accessory_summary} — the voice-input "
        "gate is open for it (accessory-only voice input: issue #2205). "
        f"Local probe: {result.detail}",
        reason=result.reason,
    )


@doctor_check(label="microphone")
def check_microphone() -> CheckResult:
    """Single headline for microphone presence.

    Reads the reconciler's one canonical record
    (``jasper.mic_presence.read_mic_presence``); the ``mic ALSA card`` / ``mic
    capture`` checks defer to the same verdict rather than re-probing ALSA.
    Absent is ``warn``, never ``fail``: voice is parked and auto-starts when a
    mic returns.

    ``ok`` claims only that the voice-input start gate is open — not that
    jasper-voice is running, and not that a *local* mic exists (the record is
    the OR of the local and accessory halves). ``mic ALSA card`` and ``mic
    capture`` are the surfaces that can tell those apart (issue #2205)."""
    mp = evidence.mic_presence()
    if mp.absent_confirmed:
        return CheckResult("microphone", "warn", mp.summary, reason=REASON_MIC_ABSENT)
    return CheckResult("microphone", "ok", mp.summary)


@doctor_check(label="mic ALSA card", needs_cfg=True)
def check_mic_card_matches_config(cfg: Config) -> CheckResult:
    """Validate the card configured in JASPER_MIC_DEVICE is actually present.

    Named cards (``Array``, ``CARD=UMIK-2``, ``plughw:CARD=Foo``) and the
    positional shorthand (``hw:7,1``) take different lookup paths. install.sh
    autodetects on the Pi, and with the AEC bridge enabled the mic moves to a
    UDP-form device (`udp:9876`), which skips the card check."""
    parked = _parked_follower_result("mic ALSA card")
    if parked is not None:
        return parked
    # No usable mic: the reconciler's single source of truth already classified
    # this and parked voice, so defer to the `microphone` headline rather than
    # re-probing `arecord -L` for a red FAILURE on an expected, auto-recovering
    # state. See jasper/mic_presence.py.
    presence = evidence.mic_presence()
    if presence.absent_confirmed:
        return CheckResult(
            "mic ALSA card", "skipped",
            "no usable microphone input — see the `microphone` check, which "
            "owns the verdict; voice stays parked until it is resolved",
            reason=REASON_MIC_ABSENT_DEFERRED,
        )
    # UDP transport has no ALSA card to validate; `check_aec_bridge_running`
    # (jasper/cli/doctor/aec.py) covers transport liveness.
    from jasper.audio_io import parse_udp_device
    try:
        if parse_udp_device(cfg.mic_device or ""):
            return CheckResult(
                f"mic ALSA card ({cfg.mic_device})", "skipped",
                "UDP transport, no ALSA card to validate",
                reason=REASON_MIC_UDP_TRANSPORT,
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
        return _soften_for_push_to_talk(CheckResult(
            label, "fail",
            f"no card {card} / device {device} in `arecord -l` output. "
            f"The AEC bridge migrated to UDP in PR 2 and the old "
            f"LoopbackAEC card no longer exists — update "
            f"JASPER_MIC_DEVICE to `udp:9876` (or `Array` for chip-direct). "
            f"Verify with `systemctl status jasper-aec-bridge`.",
            reason=REASON_MIC_CARD_ABSENT,
        ), presence)
    card = _extract_card_name(cfg.mic_device)
    if card is None:
        return CheckResult(
            "mic ALSA card",
            "warn",
            f"JASPER_MIC_DEVICE='{cfg.mic_device}' is empty or numeric; "
            "skipping name check (open test will still run)",
            reason=REASON_MIC_DEVICE_UNNAMED,
        )
    return _soften_for_push_to_talk(
        check_alsa_card(card, "arecord", f"mic ALSA card ({card})"), presence,
    )

@doctor_check()
def check_loopback() -> CheckResult:
    """snd-aloop must be loaded — on both couplings, hence `fail`.

    A `loopback`-coupled box runs its entire program path over this
    card. A ring-coupled box still needs it for every lane the ring has
    not taken; the pair allocation lives canonically in
    `deploy/modprobe.d/snd-aloop.conf` (and is cross-referenced from
    `deploy/alsa/asoundrc.jasper`).
    """
    proc = _run(["aplay", "-L"])
    if "CARD=Loopback" in proc.stdout:
        return CheckResult("snd-aloop", "ok", "CARD=Loopback present")
    return CheckResult(
        "snd-aloop", "fail",
        "Loopback device missing. `sudo modprobe snd-aloop` or check "
        "/etc/modules-load.d/snd-aloop.conf",
        reason=REASON_LOOPBACK_MISSING,
    )


@doctor_check(label="CamillaDSP websocket", needs_cfg=True, is_async=True)
async def check_camilla_websocket(cfg: Config) -> CheckResult:
    controller: CamillaController | None = None
    try:
        controller = CamillaController(cfg.camilla_host, cfg.camilla_port)
        vol = await controller.get_volume_db()
        if vol is None:
            raise CamillaUnavailable("main volume unavailable")
        try:
            clipped = await controller.get_clipped_samples()
            clipped_msg = f" clipped_samples={clipped}"
        except (
            CamillaUnavailable, OSError, RuntimeError, TimeoutError, ValueError,
        ):
            clipped_msg = " clipped_samples=?"
        if float(vol) > DEFAULT_VOLUME_LIMIT_DB + 0.1:
            return CheckResult(
                "CamillaDSP websocket", "fail",
                f"{cfg.camilla_host}:{cfg.camilla_port} volume={vol:.1f} dB "
                f"above {DEFAULT_VOLUME_LIMIT_DB:.1f} dB safety ceiling."
                f"{clipped_msg}",
                reason=REASON_CAMILLA_VOLUME_ABOVE_CEILING,
            )
        return CheckResult(
            "CamillaDSP websocket", "ok",
            f"{cfg.camilla_host}:{cfg.camilla_port} volume={vol:.1f} dB"
            f"{clipped_msg}",
        )
    except (
        CamillaUnavailable, ImportError, OSError, RuntimeError,
        TimeoutError, ValueError,
    ) as e:
        return CheckResult(
            "CamillaDSP websocket", "fail",
            f"can't reach {cfg.camilla_host}:{cfg.camilla_port}: {e}. "
            f"Check `systemctl status jasper-camilla`.",
            reason=REASON_CAMILLA_UNREACHABLE,
        )
    finally:
        if controller is not None:
            await controller.close()

def _jasper_voice_active() -> bool:
    """True if jasper-voice.service reports active."""
    return evidence.unit_active("jasper-voice.service") is True

@doctor_check(label="mic capture", needs_cfg=True, exclusive_group="audio-probe")
def check_mic_capture(cfg: Config) -> CheckResult:
    """Probe-open the mic device to confirm it produces non-silent audio.

    When jasper-voice is running it holds the mic and snd-aloop's
    exclusive-capture variants refuse a second opener; the daemon's continued
    operation IS the evidence, so that case is skipped rather than failed. UDP
    devices (the AEC bridge transport) are not PortAudio devices and skip the
    same way.
    """
    parked = _parked_follower_result("mic capture")
    if parked is not None:
        return parked
    # Intentionally idle, not broken: the reconciler's single source of truth
    # confirms no usable mic and parked jasper-voice, so defer to the
    # `microphone` headline. A genuine open failure (no absent verdict but the
    # device won't open — custom or busy mic) still falls through to the probe
    # and its fail below. See jasper/mic_presence.py.
    presence = evidence.mic_presence()
    if presence.absent_confirmed:
        return CheckResult(
            "mic capture", "skipped",
            "no usable microphone input — see the `microphone` check, which "
            "owns the verdict; voice stays parked until it is resolved",
            reason=REASON_MIC_ABSENT_DEFERRED,
        )
    # UDP transport: no PortAudio probe possible. `check_aec_bridge_running`
    # (jasper/cli/doctor/aec.py) already covers whether the transport is alive.
    from jasper.audio_io import parse_udp_device
    try:
        if parse_udp_device(cfg.mic_device or ""):
            return CheckResult(
                "mic capture", "skipped",
                f"UDP transport ({cfg.mic_device}); "
                "see `jasper-aec-bridge` for liveness",
                reason=REASON_MIC_UDP_TRANSPORT,
            )
    except ValueError as e:
        return CheckResult(
            "mic capture", "fail",
            f"malformed UDP device {cfg.mic_device!r}: {e}",
            reason=REASON_MIC_DEVICE_MALFORMED,
        )
    try:
        import numpy as np
        import sounddevice as sd
        # Open at the device's configured native rate/channels — PortAudio
        # rejects rates the device doesn't support (MicCapture downsamples to
        # 16 kHz at runtime). A half-second read is enough here.
        rec = sd.rec(
            int(0.5 * cfg.mic_capture_rate),
            samplerate=cfg.mic_capture_rate,
            channels=cfg.mic_capture_channels,
            dtype="int16", device=cfg.mic_device, blocking=True,
        )
        peak = int(np.abs(rec).max())
        if peak == 0:
            # NOT softened: the device opened, so a local microphone IS present
            # — it is muted or misrouted, which a paired accessory does not
            # make expected.
            return CheckResult(
                "mic capture", "fail",
                f"recorded silence from {cfg.mic_device} — wrong device or muted",
                reason=REASON_MIC_CAPTURE_SILENT,
            )
        if peak < 100:
            return CheckResult(
                "mic capture", "warn",
                f"recording from {cfg.mic_device} but signal is very low (peak={peak})",
                reason=REASON_MIC_CAPTURE_LOW_SIGNAL,
            )
        return CheckResult("mic capture", "ok", f"peak={peak} from {cfg.mic_device}")
    except (ImportError, OSError, RuntimeError, ValueError) as e:
        if _jasper_voice_active():
            return CheckResult(
                "mic capture", "skipped",
                f"jasper-voice holds {cfg.mic_device} (probe error: {e})",
                reason=REASON_MIC_HELD_BY_VOICE,
            )
        return _soften_for_push_to_talk(
            CheckResult(
                "mic capture", "fail", f"{cfg.mic_device}: {e}",
                reason=REASON_MIC_CAPTURE_OPEN_FAILED,
            ),
            presence,
        )

@doctor_check(label="tts output", needs_cfg=True)
def check_tts_open(cfg: Config) -> CheckResult:
    """Verify the TTS IPC socket is reachable.

    Connects to `cfg.tts_outputd_socket` (fan-in solo, outputd when
    bonded); does not send anything, since a probe write would race the
    running jasper-voice writer."""
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
            f"{socket_path} is not reachable: {e}. "
            "Start jasper-fanin (or jasper-outputd, if bonded) and check "
            "that its TTS socket exists.",
            reason=REASON_TTS_OUTPUTD_UNREACHABLE,
        )
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

@doctor_check()
def check_output_hardware_state() -> CheckResult:
    """Surface reconciler-owned output hardware state."""

    state = evidence.output_hardware_state()
    if state is None:
        return CheckResult(
            "Output hardware state",
            "warn",
            "state file unavailable — run `sudo systemctl start jasper-audio-hardware-reconcile`",
            reason=REASON_OUTPUT_HARDWARE_STATE_UNAVAILABLE,
        )
    blocker_codes = [
        str(item.get("code") or "unknown")
        for item in state.issues
        if item.get("severity") == "blocker"
    ]
    # The reconciler-emitted final-edge format (JASPER_OUTPUTD_DAC_FORMAT in
    # /var/lib/jasper/outputd.env, which env_load sources). Read, never
    # re-derived from the registry: the emitted value is what outputd and the
    # chip-AEC alignment identity actually see. Unset/blank is the S16_LE edge
    # (an unrecognized DAC, or a box predating the emit).
    final_edge = os.environ.get("JASPER_OUTPUTD_DAC_FORMAT", "").strip() or "S16_LE"
    detail = (
        f"profile={state.profile_id} status={state.status} "
        f"outputs={state.physical_output_count} apple_dacs={state.apple_dac_count} "
        f"final_edge={final_edge} (declared)"
    )
    if blocker_codes or state.status not in {"ready"}:
        return CheckResult(
            "Output hardware state",
            "fail",
            f"{detail} blocked={','.join(blocker_codes) or 'none'}",
            reason=REASON_OUTPUT_HARDWARE_BLOCKED,
        )
    return CheckResult(
        "Output hardware state",
        "ok",
        detail,
    )


@doctor_check()
def check_output_hardware_reconcile_degraded() -> CheckResult:
    """Surface a reconcile pass that skipped a probe it depends on.

    The marker is a sentinel only (no content beyond existing) — set when a
    pass could not read something it needed and cleared at the start of the
    next pass (see ``deploy/bin/jasper-audio-hardware-reconcile``)."""

    if not evidence.output_hardware_degraded():
        return CheckResult("Output hardware reconcile", "ok", "last pass completed cleanly")
    return CheckResult(
        "Output hardware reconcile",
        "warn",
        "last reconcile pass skipped a probe it depends on and left state "
        "stale — run `sudo systemctl start jasper-audio-hardware-reconcile`",
        reason=REASON_OUTPUT_HARDWARE_DEGRADED,
    )


@doctor_check()
def check_active_speaker_output_hardware_match() -> CheckResult:
    """Keep saved active-speaker topology mismatch out of basic playback health."""

    from jasper.active_speaker.runtime_contract import classify_output_contract
    from jasper.output_topology import OutputTopologyError, clock_domain_report

    try:
        topology = evidence.output_topology_strict()
    except OutputTopologyError as exc:
        return CheckResult(
            "active speaker output hardware",
            "fail",
            f"saved output topology is unavailable or invalid: {exc}",
            reason=REASON_TOPOLOGY_UNREADABLE,
        )

    contract = classify_output_contract(topology)
    if not contract.topology_configured:
        return CheckResult(
            "active speaker output hardware",
            "skipped",
            "no saved speaker topology configured",
            reason=REASON_TOPOLOGY_NOT_CONFIGURED,
        )

    observed = evidence.output_hardware_state()
    if observed is None:
        return CheckResult(
            "active speaker output hardware",
            "warn",
            "current output hardware state unavailable; run `sudo systemctl start jasper-audio-hardware-reconcile`",
            reason=REASON_OUTPUT_HARDWARE_STATE_UNAVAILABLE,
        )

    saved = topology.hardware
    saved_count = int(saved.physical_output_count or 0)
    observed_count = int(observed.physical_output_count or 0)
    detail = (
        f"saved={saved.device_id} outputs={saved_count}; "
        f"current={observed.profile_id} status={observed.status} "
        f"outputs={observed_count}"
    )
    hardware_matches = (
        saved.device_id == observed.profile_id and saved_count == observed_count
    )
    clock_blockers: list[dict[str, object]] = []
    if hardware_matches:
        clock_blockers = _observed_output_hardware_clock_blockers(
            clock_domain_report(topology)
        )
        if not clock_blockers:
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
    text = (
        f"{detail}{blocker_detail}; {suffix}. "
        "Basic output hardware is reported separately."
    )
    if hardware_matches:
        return CheckResult(
            "active speaker output hardware", status, text,
            reason=REASON_OUTPUT_HARDWARE_CLOCK_BLOCKED,
        )
    return CheckResult(
        "active speaker output hardware", status, text,
        reason=REASON_OUTPUT_HARDWARE_MISMATCH,
    )


def _output_hardware_state_or_none() -> OutputHardwareState | None:
    try:
        return evidence.output_hardware_state()
    except (OSError, ValueError, TypeError):
        return None


def _observed_output_dac_id(state: OutputHardwareState | None) -> str:
    """The DAC the reconciler last SAW, or ``unknown`` — never an env or Apple
    default. Hardware diagnostics ask this, not whether the box drives it
    (:attr:`OutputHardwareState.active_profile_id`) — a record the reconciler
    parked still names the hardware it observed."""
    return (state.observed_profile_id if state is not None else None) or "unknown"


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


CAMILLA_CONFIGS_DIR = Path("/var/lib/camilladsp/configs")


def _camilla_configs_writable_result(
    path: Path, *, expected_group: str = "jasper"
) -> CheckResult:
    """CheckResult for the CamillaDSP config dir's group-write posture.

    ``jasper-web`` runs non-root and writes staged/commissioning and
    room-correction configs into this dir atomically (temp file in-dir +
    rename), which needs directory group-write. install.sh's intended posture
    is ``root:jasper 2775``; anything narrower fails staging with
    ``PermissionError`` at the wizard instead of here."""

    label = "CamillaDSP config dir writable"
    try:
        st = path.stat()
    except FileNotFoundError:
        return CheckResult(
            label, "warn", f"{path} missing — re-run install.sh",
            reason=REASON_CAMILLA_CONFIG_DIR_MISSING,
        )
    except OSError as exc:
        return CheckResult(
            label, "warn", f"{path}: {exc}",
            reason=REASON_CAMILLA_CONFIG_DIR_UNREADABLE,
        )

    writable, group_name = _group_writable_dir(st, expected_group=expected_group)
    mode = st.st_mode & 0o7777
    detail = f"{path} mode={mode:04o} group={group_name}"
    if not writable:
        return CheckResult(
            label,
            "fail",
            f"{detail} — non-root jasper-web cannot write staged/correction "
            f"configs; fix with `sudo install -d -m 2775 -g {expected_group} "
            f"{path}` and redeploy (active-speaker staging fails with "
            "PermissionError otherwise)",
            reason=REASON_CAMILLA_CONFIG_DIR_NOT_WRITABLE,
        )
    return CheckResult(label, "ok", detail)


@doctor_check()
def check_camilla_configs_writable() -> CheckResult:
    """Guard the CamillaDSP config dir's group-write posture for jasper-web."""

    return _camilla_configs_writable_result(CAMILLA_CONFIGS_DIR)


@doctor_check()
def check_dac_usb_sync_mode() -> CheckResult:
    """Classify the speaker DAC's USB sync mode as an advisory clock-coherence
    observation for chip-AEC.

    NOT the chip-AEC gate: the binding production gate is the fixed DAC-profile
    qualification (`resolve_chip_aec_dac_gate`), and an async-but-approved DAC
    still passes it. This is an observation that helps explain a chip-AEC
    verdict, never an enable/disable switch.

    Chip-AEC assumes the output and the mic reference share a clock domain: a
    synchronous or adaptive (host-paced) USB playback endpoint keeps the DAC on
    the host clock; an asynchronous one runs its own crystal and can drift. The
    tag is read once by the output-hardware reconciler from
    /proc/asound/card<N>/stream0 into ``child_devices[*].endpoint_sync``; this
    only classifies it, against the selected output DAC's card. I2S/HAT DACs
    have no USB endpoint and are clock slaves on the I2S bus.
    """
    if not xvf3800.is_present():
        return CheckResult(
            "DAC USB sync mode", "skipped",
            "no XVF3800 mic present, chip-AEC not applicable",
            reason=REASON_DAC_SYNC_NOT_APPLICABLE,
        )

    state = _output_hardware_state_or_none()
    dac_id = _observed_output_dac_id(state)
    if state is None:
        return CheckResult(
            "DAC USB sync mode", "warn",
            "output hardware state unavailable — run "
            "`sudo systemctl start jasper-audio-hardware-reconcile`",
            reason=REASON_OUTPUT_HARDWARE_STATE_UNAVAILABLE,
        )

    # Sync tags across the DAC's playback child cards (one for a single DAC,
    # two for the dual-Apple pair). I2S DACs report "" (no USB tag).
    syncs = [
        (child.card_id, (child.endpoint_sync or "").upper())
        for child in state.child_devices
        if child.has_playback
    ]
    if not syncs:
        return CheckResult(
            "DAC USB sync mode", "warn",
            f"no playback child cards in output state (profile={dac_id})",
            reason=REASON_DAC_SYNC_NO_PLAYBACK_CARDS,
        )

    # I2S / HAT DAC: a known DAC profile with no USB endpoint sync tag — its
    # clock coherence is governed by the I2S frame clock, not a USB tag.
    if all(tag == "" for _card, tag in syncs):
        if dac_id not in {"", "unknown"}:
            return CheckResult(
                "DAC USB sync mode", "skipped",
                f"{dac_id} is not a USB DAC (I2S clock slave); "
                "USB sync mode does not gate chip-AEC",
                reason=REASON_DAC_SYNC_I2S,
            )
        return CheckResult(
            "DAC USB sync mode", "warn",
            "no USB endpoint sync tag and DAC profile is unknown",
            reason=REASON_DAC_SYNC_TAG_ABSENT,
        )

    async_cards = [card for card, tag in syncs if tag == "ASYNC"]
    coherent = [
        f"{card}:{tag}" for card, tag in syncs if tag in {"SYNC", "ADAPTIVE"}
    ]
    if async_cards:
        # Advisory only: an async endpoint is a weak clock-coherence signal,
        # but the binding chip-AEC gate is fixed DAC qualification, not this
        # tag. WARN so a maintainer notices the drift risk; software AEC3 keeps
        # echo cancelled either way.
        return CheckResult(
            "DAC USB sync mode", "warn",
            "async USB playback endpoint — weak clock coherence; chip-AEC is "
            "still gated by fixed DAC-profile qualification "
            f"(async on {','.join(async_cards)}; profile={dac_id})",
            reason=REASON_DAC_SYNC_ASYNC,
        )
    return CheckResult(
        "DAC USB sync mode", "ok",
        f"synchronous USB playback endpoint ({', '.join(coherent)}); "
        "clock-coherence observation only — production chip-AEC is gated by "
        f"fixed DAC-profile qualification (profile={dac_id})",
    )


@doctor_check()
def check_apple_dongle_audio() -> CheckResult:
    """Apple's USB-C → 3.5mm adapter exposes its USB Audio class interface only
    when something is plugged into the analog jack, so with no analog load
    lsusb sees the chip while no audio card enumerates and the reconciler's
    record names no DAC. The record is read for OBSERVED hardware, not for
    whether the reconciler is driving it.
    """
    state = _output_hardware_state_or_none()
    dac_id = _observed_output_dac_id(state)
    if dac_id != "unknown" and not _apple_output_profile_active(dac_id):
        return CheckResult(
            "Apple dongle", "skipped",
            f"active output DAC is {dac_id}",
            reason=REASON_APPLE_DONGLE_NOT_APPLICABLE,
        )

    # With nothing in its 3.5mm jack the dongle enumerates no audio card, so a
    # record naming no DAC cannot see it; the USB bus can.
    profile = _dac_profile_for(
        APPLE_USB_C_DONGLE_DEVICE_ID if dac_id == "unknown" else dac_id
    )
    usb_ids = profile.usb_ids if profile is not None else ()
    p = _run(["lsusb"])
    usb_count = sum(
        len(re.findall(re.escape(usb_id), p.stdout, re.IGNORECASE))
        for usb_id in usb_ids
    )
    if dac_id == "unknown":
        if usb_count == 0:
            return CheckResult(
                "Apple dongle", "skipped",
                "no active output DAC and no Apple dongle on USB",
                reason=REASON_APPLE_DONGLE_ABSENT,
            )
        return CheckResult(
            "Apple dongle", "warn",
            f"{usb_count} Apple USB-C adapter(s) on USB but the output-hardware "
            "record names no audio card for them. Plug speakers/headphones into "
            "the dongle's 3.5mm jack — the chip stays in low-power mode without "
            "an analog load — then run "
            "`sudo systemctl start jasper-audio-hardware-reconcile`.",
            reason=REASON_APPLE_DONGLE_NO_AUDIO_CARD,
        )
    expected_count = 2 if dac_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID else 1
    if usb_count < expected_count:
        return CheckResult(
            "Apple dongle", "fail",
            f"expected {expected_count} Apple USB-C adapter(s), "
            f"but lsusb shows {usb_count}",
            reason=REASON_APPLE_DONGLE_USB_MISSING,
        )
    cards = _apple_dongle_cards_from_state(state)
    if len(cards) >= expected_count:
        return CheckResult(
            "Apple dongle",
            "ok",
            f"USB + audio interfaces present ({','.join(cards)})",
        )
    return CheckResult(
        "Apple dongle",
        "warn",
        f"USB present but only {len(cards)} Apple audio card(s) enumerated; "
        "check analog loads on the 3.5mm jack(s).",
        reason=REASON_APPLE_DONGLE_CARDS_MISSING,
    )

_AMIXER_PERCENT_RE = re.compile(r"\[(\d+)%\]")
_CGET_VALUE_RE = re.compile(r"^\s*:\s*values=([^,\s]+)", re.M)
_CGET_ITEM_RE = re.compile(r"^\s*;\s*Item #(\d+) '(.*)'\s*$", re.M)


def _mixer_pin_problem(card_id: str, control: MixerControl) -> str | None:
    """None when this pin is held, else a short ``card:control=observed``.

    A pin that cannot be read or parsed is reported as not held: an
    unverifiable hardware gain stage is the condition this check exists for.
    """

    if control.target_percent is not None:
        probe = _run(["amixer", "-c", card_id, "sget", control.name])
        if probe.returncode != 0:
            return f"{card_id}:{control.name}=unreadable"
        percents = _AMIXER_PERCENT_RE.findall(probe.stdout)
        if not percents:
            return f"{card_id}:{control.name}=unparsed"
        # Every channel amixer prints, not just the first: one leg of a stereo
        # pair left low is the same lost loudness as both.
        off = [pct for pct in percents if int(pct) != control.target_percent]
        if off:
            return f"{card_id}:{control.name}={','.join(off)}%"
        if control.unmute and "[off]" in probe.stdout:
            return f"{card_id}:{control.name}=muted"
        return None
    probe = _run(["amixer", "-c", card_id, "cget", f"name={control.name}"])
    if probe.returncode != 0:
        return f"{card_id}:{control.name}=unreadable"
    match = _CGET_VALUE_RE.search(probe.stdout)
    if match is None or not match.group(1).lstrip("-").isdigit():
        return f"{card_id}:{control.name}=unparsed"
    observed = int(match.group(1))
    if control.target_db is not None:
        expected = _mixer_index_for_db(probe.stdout, control.target_db)
        if expected is None:
            return f"{card_id}:{control.name}=no_db_scale"
        if observed != expected:
            return f"{card_id}:{control.name}={observed}!={expected}"
        return None
    items = {name: int(index) for index, name in _CGET_ITEM_RE.findall(probe.stdout)}
    expected_item = items.get(control.target_enum or "")
    if expected_item is None:
        return f"{card_id}:{control.name}=no_such_item"
    if observed != expected_item:
        return f"{card_id}:{control.name}={observed}!={expected_item}"
    return None


@doctor_check(exclusive_group="audio-probe")
def check_dac_mixer_pins() -> CheckResult:
    """Every mixer control the observed output DAC's profile declares must sit
    at its declared pin. JTS owns gain in CamillaDSP (main_volume), so a
    hardware stage anywhere else is either lost loudness — an Apple dongle's
    Headphone left at 40% costs 36 dB with nothing else red — or, on a Studio
    board whose driver writes no defaults of its own, unrequested gain of up
    to +24 dB.

    `jasper-dac-init.service` applies these at boot and after every reconcile
    pass; this check is what catches a pin that did not hold."""
    state = _output_hardware_state_or_none()
    dac_id = _observed_output_dac_id(state)
    pins = _mixer_pins_for_state(state)
    if not pins:
        return CheckResult(
            "DAC mixer pins", "skipped",
            f"output DAC {dac_id} declares no pinned mixer controls",
            reason=REASON_DAC_MIXER_PINS_NOT_APPLICABLE,
        )
    problems = [
        problem
        for card_id, control in pins
        if (problem := _mixer_pin_problem(card_id, control)) is not None
    ]
    if problems:
        extra = len(problems) - 4
        suffix = f", +{extra} more" if extra > 0 else ""
        return CheckResult(
            "DAC mixer pins", "fail",
            f"{len(problems)} of {len(pins)} declared mixer pins are not held "
            f"on {dac_id} ({', '.join(problems[:4])}{suffix}). "
            "Run `sudo systemctl restart jasper-dac-init`.",
            reason=REASON_DAC_MIXER_PINS_NOT_HELD,
        )
    return CheckResult(
        "DAC mixer pins", "ok",
        f"{len(pins)} declared mixer pins held on {dac_id}",
    )


def _devices_volume_limit_from_text(text: str) -> float | None:
    """``devices.volume_limit`` from a CamillaDSP config, or None if absent /
    null. Uses the depth-aware shared devices parser so a nested capture or
    playback field cannot masquerade as the global fader ceiling."""
    value = parse_camilla_devices_config(text).get("volume_limit")
    if value is None:
        return None
    return float(value)

@doctor_check()
def check_camilla_volume_limit() -> CheckResult:
    """Verify the active Camilla config has JTS's non-positive fader cap."""
    config_path = evidence.camilla_config_path()
    if config_path is None:
        return CheckResult(
            "CamillaDSP volume_limit", "warn",
            f"could not read config_path from {camilla_statefile_path()}",
            reason=REASON_CAMILLA_STATEFILE_UNREADABLE,
        )
    path = Path(config_path)
    if not path.exists():
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"statefile points at missing config {config_path}",
            reason=REASON_CAMILLA_CONFIG_MISSING,
        )
    text = evidence.camilla_config_text()
    if text is None:
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"could not read {config_path}",
            reason=REASON_CAMILLA_CONFIG_UNREADABLE,
        )
    try:
        limit = _devices_volume_limit_from_text(text)
    except ValueError as e:
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"invalid devices.volume_limit in {config_path}: {e}",
            reason=REASON_VOLUME_LIMIT_INVALID,
        )
    if limit is None:
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"{config_path} omits devices.volume_limit; CamillaDSP "
            "defaults to +50 dB",
            reason=REASON_VOLUME_LIMIT_ABSENT,
        )
    if limit > DEFAULT_VOLUME_LIMIT_DB:
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"{config_path} sets devices.volume_limit={limit:.1f} dB "
            f"(expected <= {DEFAULT_VOLUME_LIMIT_DB:.1f} dB)",
            reason=REASON_VOLUME_LIMIT_ABOVE_CEILING,
        )
    return CheckResult(
        "CamillaDSP volume_limit", "ok",
        f"{config_path} devices.volume_limit={limit:.1f} dB",
    )

@doctor_check()
def check_camilla_ring_chunk_fits() -> CheckResult:
    """Verify a ring-crossing Camilla config asks for a chunk the ring can hold.

    CamillaDSP sets ``avail_min`` to its chunksize and ALSA refuses an
    ``avail_min`` above the device's buffer, so a ring config with a chunk over
    the ring's capacity does not degrade: CamillaDSP exits at open, systemd
    restart-loops it, and the speaker emits nothing. The emitters clamp the
    resolved chunk (``resolve_camilla_latency_for_devices``), so this covers the
    one case the clamp cannot reach — a config written by an OLDER build and
    still on disk.

    Removal condition: delete this check once no supported upgrade path can
    still carry a pre-clamp config onto a box.
    """
    label = "camilla ring chunk"
    config_path = evidence.camilla_config_path()
    if config_path is None:
        return CheckResult(
            label, "warn",
            f"could not read config_path from {camilla_statefile_path()}",
            reason=REASON_CAMILLA_STATEFILE_UNREADABLE,
        )
    path = Path(config_path)
    if not path.exists():
        return CheckResult(
            label, "fail", f"statefile points at missing config {config_path}",
            reason=REASON_CAMILLA_CONFIG_MISSING,
        )
    try:
        devices = parse_camilla_devices_config(path.read_text())
    except (OSError, ValueError) as e:
        return CheckResult(
            label, "fail", f"could not read {config_path}: {e}",
            reason=REASON_CAMILLA_CONFIG_UNREADABLE,
        )

    ring_ends = [
        name
        for name in (devices.get("capture_device"), devices.get("playback_device"))
        if name in RING_PCM_DEVICES
    ]
    chunksize = devices.get("chunksize")
    if not ring_ends or chunksize is None:
        return CheckResult(
            label, "skipped",
            f"{config_path} names no ring end (chunksize={chunksize})",
            reason=REASON_RING_CHUNK_NOT_APPLICABLE,
        )
    # CamillaDSP's own ceiling on the pair: target_level <= chunksize *
    # (queuelimit + 4), measured against CamillaDSP 4.1.3 and exact across
    # chunk 128/256/512 and queuelimit 1/2/4. Checked separately because a
    # config can carry a chunk that fits the ring and STILL be refused here.
    queuelimit = devices.get("queuelimit")
    target_level = devices.get("target_level")
    if queuelimit is not None and target_level is not None:
        ceiling = int(chunksize) * (int(queuelimit) + 4)
        if int(target_level) > ceiling:
            return CheckResult(
                label, "fail",
                f"{config_path} sets devices.target_level={target_level} with "
                f"chunksize={chunksize} and queuelimit={queuelimit}; CamillaDSP "
                f"refuses a target above {ceiling} and will restart-loop. "
                "Regenerate the config: `sudo jasper-sound reconcile-current-dsp`.",
                speaker_silent=True,
                reason=REASON_RING_TARGET_LEVEL_ABOVE_CEILING,
            )

    capacity = ring_capacity_frames()
    if int(chunksize) > capacity:
        return CheckResult(
            label, "fail",
            f"{config_path} sets devices.chunksize={chunksize} on "
            f"{'/'.join(ring_ends)}, above the ring's {capacity}-frame capacity. "
            "CamillaDSP cannot open the ring with it and will restart-loop. "
            "Regenerate the config: `sudo jasper-sound reconcile-current-dsp`.",
            speaker_silent=True,
            reason=REASON_RING_CHUNK_ABOVE_CAPACITY,
        )
    # Say so when the clamp is what put this number here; otherwise the box runs
    # a chunk its own DacProfile does not declare with no on-box explanation.
    # Asked of the SAME resolver the emitters fall back to, never of a second
    # derivation of "which DAC is active".
    fits = (
        f"chunksize={chunksize} fits the ring's {capacity}-frame capacity "
        f"({'/'.join(ring_ends)})"
    )
    unclamped = resolve_camilla_chunksize()
    if unclamped > capacity:
        return CheckResult(
            label, "ok",
            f"{fits}, clamped from the {unclamped} this box resolves to",
            reason=REASON_RING_CHUNK_CLAMPED,
        )
    return CheckResult(label, "ok", fits)
