# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — audio domain."""
from __future__ import annotations

import json
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
    CheckResult,
    _group_writable_dir,
    _parked_follower_result,
    _run,
)
from .correction import (
    REASON_CAMILLA_CONFIG_MISSING,
    REASON_CAMILLA_CONFIG_UNREADABLE,
    REASON_CAMILLA_STATEFILE_UNREADABLE,
    _active_camilla_config_path,
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

REASON_TOPOLOGY_UNREADABLE = "output_topology_unreadable"
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

REASON_GRAPH_PASSIVE_LAYOUT = "runtime_graph_passive_layout"
REASON_GRAPH_PARKED_SILENT = "runtime_graph_parked_silent"
REASON_GRAPH_LAYOUT_INCOMPLETE = "runtime_graph_layout_incomplete"
REASON_GRAPH_UNCONFIGURED_NOT_PARKED = "runtime_graph_unconfigured_not_parked"
REASON_GRAPH_UNSAFE = "runtime_graph_unsafe"

REASON_SOUND_PROFILE_DEFAULT = "sound_profile_default"
REASON_SOUND_PROFILE_UNREADABLE = "sound_profile_unreadable"
REASON_SOUND_PROFILE_NOT_ACTIVE = "sound_profile_not_active"

REASON_BASS_EXTENSION_NOT_COMMISSIONED = "bass_extension_not_commissioned"
REASON_BASS_EXTENSION_MALFORMED = "bass_extension_malformed"
REASON_BASS_EXTENSION_STALE = "bass_extension_stale"
REASON_BASS_EXTENSION_BYPASSED = "bass_extension_bypassed"

REASON_DSP_APPLY_NONE = "dsp_apply_none"
REASON_DSP_APPLY_ROLLBACK_FAILED = "dsp_apply_rollback_failed"
REASON_DSP_APPLY_UNSUCCESSFUL = "dsp_apply_unsuccessful"

REASON_BASELINE_CANONICAL_NOT_APPLICABLE = "baseline_canonical_not_applicable"
REASON_BASELINE_CANONICAL_MISSING = "baseline_canonical_missing"
REASON_BASELINE_CANONICAL_LIVE_MISSING = "baseline_canonical_live_missing"
REASON_BASELINE_CANONICAL_UNCOMPARABLE = "baseline_canonical_uncomparable"
REASON_BASELINE_CANONICAL_STALE = "baseline_canonical_stale"

REASON_SPEAKER_SETUP_UNREADABLE = "speaker_setup_unreadable"
REASON_APPLIED_GRAPH_NO_PROFILE = "applied_graph_no_profile"
REASON_APPLIED_GRAPH_STAGED_ANCHOR = "applied_graph_staged_anchor"
REASON_APPLIED_GRAPH_NOT_EVALUATED = "applied_graph_not_evaluated"
REASON_APPLIED_GRAPH_MISMATCH = "applied_graph_mismatch"

REASON_STARTUP_HOLD_NONE = "startup_hold_none"
REASON_STARTUP_HOLD_IN_FLIGHT = "startup_hold_in_flight"
REASON_STARTUP_HOLD_STALE = "startup_hold_stale"

REASON_ROOM_AUTHORITY_NO_DECISION = "room_authority_no_decision"
REASON_ROOM_AUTHORITY_NOT_REQUIRED = "room_authority_not_required"
REASON_ROOM_AUTHORITY_UNBANKED = "room_authority_unbanked"
REASON_ROOM_AUTHORITY_RECEIPT_UNREADABLE = "room_authority_receipt_unreadable"
REASON_ROOM_AUTHORITY_UNPROVEN = "room_authority_unproven"

REASON_SETUP_NOTICES_NONE = "setup_notices_none"
REASON_SETUP_NOTICES_STANDING = "setup_notices_standing"


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
def check_active_speaker_output_hardware_match() -> CheckResult:
    """Keep saved active-speaker topology mismatch out of basic playback health."""

    from jasper.active_speaker.runtime_contract import classify_output_contract
    from jasper.output_topology import OutputTopologyError, clock_domain_report

    try:
        topology = _output_topology_strict()
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


def _output_topology_strict():
    """The fail-closed topology load (ADR-0233 rule 4), read once per run.

    Distinct from ``evidence.output_topology()``: that variant fails soft to
    an empty draft, which the safety-authorizing callers here must not do —
    they need to see (and fail on) a corrupt/unreadable saved topology.
    """
    from ...output_topology import load_output_topology_strict

    return evidence.get("output_topology_strict", load_output_topology_strict)


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


_SPEAKER_SETUP_URL = "http://<speaker>/sound/setup/"


def _blocker_summary(contract) -> str:
    """``blockers=<codes>: <messages>`` for a contract, empty when it is clean."""

    if not contract.issues:
        return ""
    codes = ",".join(str(issue.get("code") or "") for issue in contract.issues)
    messages = "; ".join(
        str(issue.get("message") or "")
        for issue in contract.issues
        if issue.get("message")
    )
    return f"blockers={codes}" + (f": {messages}" if messages else "")


def _incomplete_layout_detail(contract) -> str:
    """Why the saved layout is not a complete passive one, and where to fix it."""

    blocker = (
        contract.issues[0]["message"]
        if contract.issues
        else "saved layout is not a complete passive mono or stereo layout"
    )
    return f"{blocker}. Fix the layout at {_SPEAKER_SETUP_URL}"


@doctor_check()
def check_active_speaker_runtime_graph() -> CheckResult:
    """Report the graph selected for saved speaker intent, fail closed if unsafe.

    "Is the speaker parked" is answered by ``active_graph_is_parked`` and the
    way out by ``parked_muted_exits`` — the readers ``/state`` and
    ``jasper.control.audio_health`` consume (ADR-0233 rule 1). Asked of the
    file the safety proof classified, not of a second statefile resolution, so
    one row never mixes two views of the disk. Deliberately narrower than those
    two reporting surfaces in one direction: bytes carrying the parked
    provenance marker that FAIL the structural all-muted proof are reported
    here as unsafe, never as a healthy park.

    The saved layout's unresolved blockers ride on this row rather than a
    second one: a blocker is already a refusal of the runtime graph, and on a
    parked box it needs saying that clearing them does not by itself restore
    sound — parking is gated on the absence of a staged startup graph.

    Parked is WARN, never FAIL (#2145): a parked speaker is silent, not broken,
    and a mid-commission box must stay deployable.
    """
    from jasper.active_speaker.runtime_contract import (
        CONTRACT_UNCONFIGURED,
        active_graph_is_parked,
        classify_bass_extension_graph,
        classify_output_contract,
        parked_muted_exits,
        topology_allows_flat_dac_graph,
    )
    from jasper.output_topology import OutputTopologyError

    name = "active speaker runtime graph"
    try:
        topology = _output_topology_strict()
    except OutputTopologyError as exc:
        return CheckResult(
            name, "fail",
            f"saved output topology is unavailable or invalid: {exc}",
            reason=REASON_TOPOLOGY_UNREADABLE,
        )
    contract = classify_output_contract(topology)
    # The SSOT that authorizes a flat DAC graph is deliberately narrower than
    # "not roleful": unconfigured and incomplete/invalid non-roleful layouts
    # are not passive playback contracts.
    if topology_allows_flat_dac_graph(contract):
        return CheckResult(
            name, "ok",
            f"{contract.classification}: explicit passive layout is valid",
            reason=REASON_GRAPH_PASSIVE_LAYOUT,
        )

    statefile, config_path = evidence.get("camilla_config", _active_camilla_config_path)
    if config_path is None:
        return CheckResult(
            name, "fail",
            (
                f"could not read config_path from {statefile}; saved topology "
                "does not permit an unchecked flat fallback"
            ),
            reason=REASON_CAMILLA_STATEFILE_UNREADABLE,
        )
    if not Path(config_path).exists():
        return CheckResult(
            name, "fail",
            f"statefile points at missing config {config_path}",
            reason=REASON_CAMILLA_CONFIG_MISSING,
        )
    from ...active_speaker.state_paths import baseline_profile_state_path
    from ...active_speaker.staging import staged_metadata_path
    from ...bass_extension import BASS_EXTENSION_APPLY_INTENT_PATH
    from ...bass_extension.profile import DEFAULT_PROFILE_PATH

    graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=Path(statefile),
        applied_baseline_path=baseline_profile_state_path(),
        profile_path=DEFAULT_PROFILE_PATH,
        intent_path=BASS_EXTENSION_APPLY_INTENT_PATH,
        staged_metadata_path=staged_metadata_path(),
    )
    if graph.allowed and active_graph_is_parked(graph.config_path):
        # A parked graph is intentional silence, not a broken runtime — both a
        # zero-group topology (the household must choose a layout) and an
        # incomplete roleful layout. Either way the proof above establishes that
        # every output is muted, so both arms below carry `speaker_silent`.
        if contract.classification == CONTRACT_UNCONFIGURED or (
            contract.requires_roleful_graph
        ):
            blockers = _blocker_summary(contract)
            return CheckResult(
                name, "warn",
                f"parked silent for {contract.classification}."
                + (f" Clear {blockers} at {_SPEAKER_SETUP_URL}." if blockers else "")
                + f" Next: {parked_muted_exits(topology)}",
                speaker_silent=True,
                reason=REASON_GRAPH_PARKED_SILENT,
            )
        return CheckResult(
            name, "fail", _incomplete_layout_detail(contract),
            speaker_silent=True,
            reason=REASON_GRAPH_LAYOUT_INCOMPLETE,
        )
    if graph.allowed:
        if contract.classification == CONTRACT_UNCONFIGURED:
            return CheckResult(
                name, "fail",
                "unconfigured topology must use the proved parked graph",
                reason=REASON_GRAPH_UNCONFIGURED_NOT_PARKED,
            )
        if not contract.requires_roleful_graph:
            return CheckResult(
                name, "fail", _incomplete_layout_detail(contract),
                reason=REASON_GRAPH_LAYOUT_INCOMPLETE,
            )
        return CheckResult(
            name, "ok",
            f"{graph.classification} is legal for {contract.classification}",
        )

    detail = (
        graph.issues[0]["message"]
        if graph.issues
        else "Camilla graph is unsafe for saved active speaker topology"
    )
    return CheckResult(name, "fail", detail, reason=REASON_GRAPH_UNSAFE)


def _sound_profile_path() -> Path:
    return Path(
        os.environ.get(
            "JASPER_SOUND_PROFILE_PATH",
            "/var/lib/jasper/sound_profile.json",
        )
    )

@doctor_check()
def check_sound_profile() -> CheckResult:
    from jasper.sound.camilla_yaml import is_jts_generated_config
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
            reason=REASON_SOUND_PROFILE_DEFAULT,
        )
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return CheckResult(
            "sound profile", "fail", f"could not read {path}: {e}",
            reason=REASON_SOUND_PROFILE_UNREADABLE,
        )

    profile = SoundProfile.from_mapping(raw)
    filter_count = len(build_sound_filters(profile))
    headroom_db = estimate_headroom_db(profile)
    settings = load_sound_settings()
    trim = output_trim_db(profile, settings)

    active_path = evidence.camilla_config_path()
    # "Is this a JTS-generated config?" has ONE owner
    # (:func:`jasper.sound.camilla_yaml.is_jts_generated_config`) — never a
    # local copy of the name set: since #2572 the reconcile legitimately leaves
    # a content-identical graph running under whatever it is named instead of
    # rewriting it to `sound_current.yml`, so a stale copy here would
    # permanently tell a household its saved profile is missing from a graph
    # that carries it. `config_dir` is the active config's own parent, the same
    # way `check_correction_current_config` asks, so the only question left to
    # the canonical owner is the name.
    active_generated = is_jts_generated_config(
        active_path,
        config_dir=Path(active_path).parent,
    ) if active_path else False
    drifted = bool(profile.enabled and filter_count and not active_generated)

    detail = (
        f"enabled={profile.enabled} curve={profile.curve_id} "
        f"filters={filter_count} headroom={headroom_db:.1f}dB "
        f"match_loudness={'on' if settings.match_loudness else 'off'} "
        f"output_trim={trim:.1f}dB"
        + (" (saved profile not reflected in active generated config)"
           if drifted else "")
    )
    if drifted:
        return CheckResult(
            "sound profile", "warn", detail,
            reason=REASON_SOUND_PROFILE_NOT_ACTIVE,
        )
    return CheckResult("sound profile", "ok", detail)

@doctor_check()
def check_bass_extension_profile() -> CheckResult:
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )
    from jasper.bass_extension.profile import evaluate_bass_extension_profile

    evaluation = evaluate_bass_extension_profile(
        topology=evidence.output_topology(),
        applied_baseline_state=load_applied_baseline_profile_state(),
    )
    if evaluation.status == "missing":
        return CheckResult(
            "bass extension profile", "ok", "bass extension: not commissioned",
            reason=REASON_BASS_EXTENSION_NOT_COMMISSIONED,
        )
    if evaluation.status == "malformed":
        return CheckResult(
            "bass extension profile",
            "fail",
            f"bass extension profile is malformed: {evaluation.detail}",
            reason=REASON_BASS_EXTENSION_MALFORMED,
        )
    if evaluation.status == "stale":
        refusals = ",".join(refusal.value for refusal in evaluation.refusals)
        return CheckResult(
            "bass extension profile",
            "warn",
            f"bass extension profile is stale [{refusals}]: {evaluation.detail}",
            reason=REASON_BASS_EXTENSION_STALE,
        )
    if evaluation.status == "bypassed":
        return CheckResult(
            "bass extension profile", "ok", "bass extension profile is bypassed",
            reason=REASON_BASS_EXTENSION_BYPASSED,
        )
    assert evaluation.profile is not None
    return CheckResult(
        "bass extension profile",
        "ok",
        f"accepted; deepest={evaluation.profile.targets[0].fp_hz:g}Hz "
        f"natural={evaluation.profile.targets[-1].fp_hz:g}Hz",
    )

@doctor_check()
def check_dsp_apply_state() -> CheckResult:
    from jasper.dsp_apply import last_dsp_apply_state

    state = last_dsp_apply_state()
    if state is None:
        return CheckResult(
            "DSP apply state",
            "ok",
            "no DSP apply attempts recorded yet",
            reason=REASON_DSP_APPLY_NONE,
        )

    result = str(state.get("result") or "unknown")
    phase = str(state.get("phase") or "unknown")
    source = str(state.get("source") or "unknown")
    candidate = state.get("candidate_config_path")
    op_id = str(state.get("op_id") or "")[:8]

    detail = f"source={source} result={result} phase={phase} op={op_id}"
    if candidate:
        detail += f" config={candidate}"
    if state.get("rollback_attempted") and state.get("rollback_succeeded") is False:
        return CheckResult(
            "DSP apply state", "fail", detail,
            reason=REASON_DSP_APPLY_ROLLBACK_FAILED,
        )
    if result != "success":
        return CheckResult(
            "DSP apply state", "warn", detail,
            reason=REASON_DSP_APPLY_UNSUCCESSFUL,
        )
    return CheckResult("DSP apply state", "ok", detail)

def _is_baseline_candidate_sibling(live_path: Path, canonical: Path) -> bool:
    """True if ``live_path`` is a source-fingerprinted sibling of ``canonical``.

    ``build_baseline_profile_candidate`` names every candidate
    ``<canonical stem>_candidate_<fingerprint12><canonical suffix>`` beside
    the canonical file (issue #1666). Used to gate the comparison below to
    speakers that actually have an active-speaker baseline applied live —
    a plain stereo/flat topology's live config (e.g. ``outputd-cutover.yml``)
    never matches this shape, so it stays "not applicable" rather than a
    false warning.
    """
    return (
        live_path.parent == canonical.parent
        and live_path.suffix == canonical.suffix
        and live_path.name.startswith(f"{canonical.stem}_candidate_")
    )

@doctor_check()
def check_active_speaker_baseline_canonical() -> CheckResult:
    """Canonical ``active_speaker_baseline.yml`` durability (issue #1666).

    ``build_baseline_profile_candidate`` never writes the canonical
    ``baseline_config_path()`` name directly; every apply/restore promotes the
    applied candidate's bytes onto it fail-soft, after CamillaDSP confirmed the
    candidate live. A failed promote leaves that copy stale without affecting
    the audible graph, which the other readers of the canonical name (the
    multiroom follower fallback, operators, this doctor) trust. Disclosed as
    `ok`: the live graph is the audible truth and is correct either way.
    """
    from jasper.active_speaker.baseline_profile import (
        active_layer_a_fingerprint,
        baseline_config_path,
    )
    from jasper.active_speaker.profile import ActiveSpeakerConfigError

    label = "active speaker baseline canonical"
    statefile, live_path_raw = evidence.get("camilla_config", _active_camilla_config_path)
    if live_path_raw is None:
        # A missing/unreadable outputd statefile is already a real failure at
        # the checks that own it (check_active_speaker_runtime_graph fails when
        # a roleful topology needs it). This check's scope is only "does
        # canonical mirror the live baseline", which cannot be evaluated here —
        # not applicable, not a warning.
        return CheckResult(
            label, "skipped",
            f"could not read config_path from {statefile}",
            reason=REASON_BASELINE_CANONICAL_NOT_APPLICABLE,
        )
    live_path = Path(live_path_raw)
    canonical = baseline_config_path()
    if live_path == canonical:
        return CheckResult(
            label, "ok", f"live config is the canonical file ({canonical})",
        )
    if not _is_baseline_candidate_sibling(live_path, canonical):
        return CheckResult(
            label, "skipped",
            f"live config ({live_path}) is not an active-speaker baseline "
            "candidate",
            reason=REASON_BASELINE_CANONICAL_NOT_APPLICABLE,
        )
    if not canonical.exists():
        return CheckResult(
            label, "ok",
            f"canonical baseline file is missing ({canonical}) while the live "
            f"config is an applied baseline candidate ({live_path}); the next "
            "apply or restore re-promotes it",
            reason=REASON_BASELINE_CANONICAL_MISSING,
        )
    if not live_path.exists():
        return CheckResult(
            label, "ok",
            f"live baseline candidate file is missing on disk ({live_path}); "
            f"cannot compare it against canonical ({canonical})",
            reason=REASON_BASELINE_CANONICAL_LIVE_MISSING,
        )
    try:
        live_fingerprint = active_layer_a_fingerprint(
            live_path.read_text(encoding="utf-8")
        )
        canonical_fingerprint = active_layer_a_fingerprint(
            canonical.read_text(encoding="utf-8")
        )
    except (OSError, ActiveSpeakerConfigError) as exc:
        return CheckResult(
            label, "warn", f"could not compare {live_path} to {canonical}: {exc}",
            reason=REASON_BASELINE_CANONICAL_UNCOMPARABLE,
        )
    if live_fingerprint == canonical_fingerprint:
        return CheckResult(
            label, "ok",
            f"canonical file ({canonical}) matches the live applied baseline "
            f"({live_path})",
        )
    return CheckResult(
        label, "ok",
        f"canonical baseline file ({canonical}) does not match the live "
        f"applied config ({live_path}); the running graph is correct, but the "
        "canonical file is stale for other readers (multiroom follower "
        "fallback, operators)",
        reason=REASON_BASELINE_CANONICAL_STALE,
    )


@doctor_check(label="active speaker applied graph")
def check_active_speaker_applied_graph() -> CheckResult:
    """Is the durable graph the one the applied profile names?

    A crossover-v2 round that ends on a verify rejection banks no adoption but
    has already repointed CamillaDSP's persisted ``config_file_path`` at the
    rejected candidate, leaving the anchor's per-driver values disagreeing with
    the applied profile's. ``setup_status`` binds the two; this reads the
    binding out.

    Compared at the DURABLE anchor, never at the running graph: runtime-only
    swaps (audition, ADR-0193; the measurement session graph; the per-driver
    commissioning load) install through ``set_active_config_raw`` and leave the
    statefile alone, so none can read as drift. A staged/commissioning anchor
    IS a durable repoint and is excluded by name instead.

    WARN, never FAIL: the anchor is the audible truth either way.
    """

    from ...active_speaker.setup_status import IN_SEQUENCE_CAPTURE_ANCHOR_REASON

    label = "active speaker applied graph"
    try:
        status = evidence.active_speaker_setup_status()
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return CheckResult(
            label, "warn", f"could not read speaker setup: {exc}",
            reason=REASON_SPEAKER_SETUP_UNREADABLE,
        )
    protected = status.get("protected_profile")
    binding = protected.get("layer_a_binding") if isinstance(protected, dict) else None
    if not isinstance(binding, dict):
        return CheckResult(
            label, "skipped", "no applied active-speaker profile to bind",
            reason=REASON_APPLIED_GRAPH_NO_PROFILE,
        )
    issues = status.get("issues")
    if any(
        isinstance(issue, dict)
        and issue.get("code") == IN_SEQUENCE_CAPTURE_ANCHOR_REASON
        for issue in (issues if isinstance(issues, list) else [])
    ):
        return CheckResult(
            label, "skipped",
            "a commissioning/staged graph is the durable anchor by design",
            reason=REASON_APPLIED_GRAPH_STAGED_ANCHOR,
        )
    if binding.get("matches") is True:
        return CheckResult(
            label, "ok",
            "the durable graph is the one the applied profile names "
            f"(layer_a={binding.get('loaded_fingerprint')})",
        )
    if binding.get("status") != "mismatch":
        return CheckResult(
            label, "skipped",
            "applied-profile graph binding not evaluated "
            f"({binding.get('status') or 'absent'})",
            reason=REASON_APPLIED_GRAPH_NOT_EVALUATED,
        )
    fields = "; ".join(
        f"{item.get('field')} profile={item.get('expected')} "
        f"graph={item.get('loaded')}"
        for item in (binding.get("differences") or [])
        if isinstance(item, dict)
    )
    return CheckResult(
        label, "warn",
        f"the durable graph at {status.get('active_config_path')} is not the one "
        "the applied profile names: layer_a profile="
        f"{binding.get('expected_fingerprint')} graph="
        f"{binding.get('loaded_fingerprint')}"
        + (f" [{fields}]" if fields else "")
        + " — apply that crossover again, or republish the banked candidate "
        "and apply it, to make the two agree",
        reason=REASON_APPLIED_GRAPH_MISMATCH,
    )


@doctor_check(label="active speaker startup hold")
def check_active_speaker_startup_hold() -> CheckResult:
    """A staged-startup hold marker with no startup load behind it is stale.

    ``load_protected_startup_config`` takes an ephemeral ``/run`` marker before
    applying the all-muted staged anchor, and while it is present
    ``safe_graph_for_current_topology`` preserves that anchor instead of
    restoring the saved baseline. A marker outliving its load therefore keeps a
    commissioned box SILENT across every reconcile — recoverable (a reboot or
    rollback clears it) but invisible without this line. It is what the
    household-facing ``staged_startup_hold_unavailable`` copy points at.

    FAIL with ``speaker_silent``: nothing reaches a driver while it holds.
    """

    from ...active_speaker.startup_hold import (
        staged_startup_hold_active,
        startup_hold_marker_path,
    )
    from ...active_speaker.startup_load import load_startup_load_state

    label = "active speaker startup hold"
    marker = startup_hold_marker_path()
    if not staged_startup_hold_active():
        return CheckResult(
            label, "ok", f"no staged-startup hold in flight ({marker})",
            reason=REASON_STARTUP_HOLD_NONE,
        )
    status = str(load_startup_load_state().get("status") or "unknown")
    if status == "loaded":
        return CheckResult(
            label, "ok",
            f"staged-startup hold held by an in-flight protected load ({marker})",
            reason=REASON_STARTUP_HOLD_IN_FLIGHT,
        )
    return CheckResult(
        label, "fail",
        f"stale staged-startup hold at {marker}: the startup load is "
        f"'{status}', not 'loaded', so no commission is in flight — the graph "
        "selector keeps preserving the silent all-muted anchor instead of "
        "restoring the saved baseline. Roll back the startup load from "
        "http://jts.local/sound/ or reboot to clear it (/run is tmpfs).",
        speaker_silent=True,
        reason=REASON_STARTUP_HOLD_STALE,
    )


@doctor_check(label="room correction authority")
def check_room_correction_authority() -> CheckResult:
    """Room correction runs unproven — this is the line that says so.

    Ruling S10 and ADR-0019: an unminted, stale or unreadable commissioning
    receipt no longer refuses a room-correction run — it proceeds and simply
    banks no verified result, so this is the only place a household learns the
    difference. Never FAIL. The denials do not share one line because they do
    not share a remedy (ADR-0196).
    """

    from ...active_speaker._common import (
        ROOM_AUTHORITY_RECEIPT_ABSENT,
        ROOM_AUTHORITY_RECEIPT_UNREADABLE,
    )
    label = "room correction authority"
    try:
        status = evidence.active_speaker_setup_status()
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return CheckResult(
            label, "warn", f"could not read speaker setup: {exc}",
            reason=REASON_SPEAKER_SETUP_UNREADABLE,
        )
    acoustic = status.get("acoustic_commissioning")
    if not isinstance(acoustic, dict):
        return CheckResult(
            label, "warn", "speaker setup published no room decision",
            reason=REASON_ROOM_AUTHORITY_NO_DECISION,
        )
    if acoustic.get("required") is not True:
        return CheckResult(
            label, "skipped", "room correction needs no speaker authority",
            reason=REASON_ROOM_AUTHORITY_NOT_REQUIRED,
        )
    if acoustic.get("allowed") is True:
        return CheckResult(
            label, "ok",
            f"room correction is banked under {acoustic.get('authority')}",
        )
    denial = str(acoustic.get("reason") or "")
    detail = str(acoustic.get("detail") or "")
    cause = str(acoustic.get("cause") or "")
    if denial == ROOM_AUTHORITY_RECEIPT_ABSENT:
        # The state every uncommissioned speaker is in, hence `ok`. ABSENT is
        # also the module's catch-all default, so it covers a receipt that
        # VANISHED under a verified lifecycle; forwarding `cause` keeps that
        # sub-state visible without turning it into a nag.
        return CheckResult(
            label, "ok",
            f"room correction runs unbanked ({denial})"
            + (f": {cause}" if cause else ""),
            reason=REASON_ROOM_AUTHORITY_UNBANKED,
        )
    if denial == ROOM_AUTHORITY_RECEIPT_UNREADABLE:
        # A machine fault, not a verdict on the record: the file and errno are
        # the sentence that ends the incident. Without them an operator reads
        # "unproven" and goes looking for a mint that was never the problem.
        return CheckResult(
            label, "warn",
            "room correction cannot read its commissioning record "
            f"({cause or denial}): {detail}",
            reason=REASON_ROOM_AUTHORITY_RECEIPT_UNREADABLE,
        )
    return CheckResult(
        label, "ok", f"room correction runs unproven ({denial}): {detail}",
        reason=REASON_ROOM_AUTHORITY_UNPROVEN,
    )


@doctor_check(label="active speaker setup notices")
def check_active_speaker_setup_notices() -> CheckResult:
    """The standing home for setup facts that no longer stop anything.

    Ruling S10 and ADR-0019 turn staleness and unproven-ness into disclosures
    rather than blocks; nothing else renders a non-blocker setup issue.
    Blockers keep their own surfaces (`/state`, the landing page, the volume
    and grouping refusals) and are not repeated here.
    """

    label = "active speaker setup notices"
    try:
        status = evidence.active_speaker_setup_status()
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return CheckResult(
            label, "warn", f"could not read speaker setup: {exc}",
            reason=REASON_SPEAKER_SETUP_UNREADABLE,
        )
    issues = status.get("issues")
    notices = [
        issue for issue in (issues if isinstance(issues, list) else [])
        if isinstance(issue, dict) and issue.get("severity") != "blocker"
    ]
    if not notices:
        return CheckResult(
            label, "ok", "no standing speaker setup notices",
            reason=REASON_SETUP_NOTICES_NONE,
        )
    return CheckResult(
        label, "ok",
        "; ".join(
            f"{issue.get('code')}: {issue.get('message')}" for issue in notices
        ),
        reason=REASON_SETUP_NOTICES_STANDING,
    )
