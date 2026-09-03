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
    APPLE_USB_C_DONGLE_ID,
    by_id as _dac_profile_for,
    mixer_control_groups_for as _dac_mixer_control_groups_for,
)
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
    load_state as _load_output_hardware_state,
)
from ...mic_presence import MicPresence, read_mic_presence
from ._registry import doctor_check
from ._shared import (
    CheckResult,
    _group_writable_dir,
    _parked_as_bonded_follower,
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
    voice-input gate legitimately open (issue #2205), so a red "your microphone
    is missing" is the wrong register. These checks probe the local device, so
    they are the surface that can tell "no local mic" from "local mic present";
    the `microphone` headline cannot, because it reads the OR verdict. The
    detail reports GATE state, never runtime state.

    The local finding stays visible (``warn``, original detail appended); only
    the register changes. A ``warn``/``ok`` result is returned untouched — this
    never upgrades a status.

    Applied only to *device-absent / cannot-open* failures. A mic that opens but
    records silence is a present-and-broken local mic, not an absent one; that
    stays a red failure regardless of what accessory is paired."""
    if result.status != "fail" or not presence.accessory_present:
        return result
    return CheckResult(
        result.name,
        "warn",
        f"no local microphone; {presence.accessory_summary} — the voice-input "
        "gate is open for it (accessory-only voice input: issue #2205). "
        f"Local probe: {result.detail}",
    )


@doctor_check(order=3.5, group="audio", label="microphone")
def check_microphone() -> CheckResult:
    """Single headline for microphone presence.

    Reads the reconciler's one canonical record via
    ``jasper.mic_presence.read_mic_presence``; the downstream ``mic ALSA card``
    / ``mic capture`` checks defer to the same verdict instead of re-probing
    ALSA, so a missing mic is one advisory, not a scatter of contradicting
    failures. Absent is ``warn``, never ``fail``: the reconciler parked voice
    and it auto-starts when a mic is reconnected or an actionable profile
    condition is resolved.

    ``ok`` claims only that the voice-input start gate is open. Not that
    jasper-voice is running, and not that a *local* microphone exists — the
    record is the OR of the local and accessory halves and carries no local
    probe (see ``jasper.mic_presence``). It therefore does NOT drop to ``warn``
    for an accessory-satisfied gate: the identical record shape covers a
    healthy non-XVF local mic (a custom ``JASPER_MIC_DEVICE``, a plain USB mic)
    on a box with a remote paired. ``mic ALSA card`` and ``mic capture`` are
    the surfaces that can tell those apart; they downgrade to ``warn`` naming
    issue #2205 when the local mic is genuinely missing."""
    mp = read_mic_presence()
    status = "warn" if mp.absent_confirmed else "ok"
    return CheckResult("microphone", status, mp.summary)


@doctor_check(order=4, group="audio", label="mic ALSA card", needs_cfg=True)
def check_mic_card_matches_config(cfg: Config) -> CheckResult:
    """Validate the card configured in JASPER_MIC_DEVICE is actually present.

    Named cards (``Array``, ``CARD=UMIK-2``, ``plughw:CARD=Foo``) and the
    positional shorthand (``hw:7,1``, ``plughw:0,0``) take different lookup
    paths. install.sh autodetects on the Pi, so the literal may differ from
    'Array' — e.g. when the AEC bridge is enabled the mic moves to a UDP-form
    device (`udp:9876`) and this card check is skipped."""
    if _parked_as_bonded_follower():
        return CheckResult(
            "mic ALSA card", "ok",
            "parked (bonded follower) — the dumb-follower profile stops "
            "voice + the AEC stack while paired; the leader owns the mic",
        )
    # No usable mic: the reconciler's single source of truth already classified
    # this and parked voice, so defer to the `microphone` headline rather than
    # re-probing `arecord -L` for a red FAILURE on an expected, auto-recovering
    # state. See jasper/mic_presence.py.
    presence = read_mic_presence()
    if presence.absent_confirmed:
        return CheckResult(
            "mic ALSA card", "ok",
            "no usable microphone input — see the `microphone` check "
            "(voice is intentionally parked until its condition is resolved)",
        )
    # UDP transport has no ALSA card to validate; `check_aec_bridge_running`
    # (jasper/cli/doctor/aec.py) covers transport liveness.
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
        return _soften_for_push_to_talk(CheckResult(
            label, "fail",
            f"no card {card} / device {device} in `arecord -l` output. "
            f"The AEC bridge migrated to UDP in PR 2 and the old "
            f"LoopbackAEC card no longer exists — update "
            f"JASPER_MIC_DEVICE to `udp:9876` (or `Array` for chip-direct). "
            f"Verify with `systemctl status jasper-aec-bridge`.",
        ), presence)
    card = _extract_card_name(cfg.mic_device)
    if card is None:
        return CheckResult(
            "mic ALSA card",
            "warn",
            f"JASPER_MIC_DEVICE='{cfg.mic_device}' is empty or numeric; "
            "skipping name check (open test will still run)",
        )
    return _soften_for_push_to_talk(
        check_alsa_card(card, "arecord", f"mic ALSA card ({card})"), presence,
    )

@doctor_check(order=5, group="audio")
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
    )

# order=79 stays AFTER resilience's fractional 78.5 insert — the registry
# contract is "the single async check sorts last", not contiguous integers
# (test_doctor_registry), so the gap below 79 is intentional.
@doctor_check(order=79, group="audio", label="CamillaDSP websocket", needs_cfg=True, is_async=True)
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
        )
    finally:
        if controller is not None:
            await controller.close()

def _jasper_voice_active() -> bool:
    """True if jasper-voice.service reports active."""
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

    UDP devices (`udp:N` / `udp://HOST:N`, the AEC bridge transport)
    aren't PortAudio devices — there's no `sd.rec` for them, so the
    probe is skipped the same way.
    """
    if _parked_as_bonded_follower():
        return CheckResult(
            "mic capture", "ok",
            "parked (bonded follower) — the dumb-follower profile stops "
            "voice + the AEC stack while paired; the leader owns the mic",
        )
    # Intentionally idle, not broken: the reconciler's single source of truth
    # confirms no usable mic and parked jasper-voice, so defer to the
    # `microphone` headline. A genuine open failure (no absent verdict but the
    # device won't open — custom or busy mic) still falls through to the probe
    # and its fail below. See jasper/mic_presence.py.
    presence = read_mic_presence()
    if presence.absent_confirmed:
        return CheckResult(
            "mic capture", "ok",
            "no usable microphone input (expected) — see the `microphone` "
            "check; voice is intentionally parked until its condition is resolved",
        )
    # UDP transport: no PortAudio probe possible. `check_aec_bridge_running`
    # (jasper/cli/doctor/aec.py) already covers whether the transport is alive.
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
            )
        if peak < 100:
            return CheckResult(
                "mic capture", "warn",
                f"recording from {cfg.mic_device} but signal is very low (peak={peak})",
            )
        return CheckResult("mic capture", "ok", f"peak={peak} from {cfg.mic_device}")
    except (ImportError, OSError, RuntimeError, ValueError) as e:
        if _jasper_voice_active():
            return CheckResult(
                "mic capture", "ok",
                f"skipped — jasper-voice holds {cfg.mic_device} (probe error: {e})",
            )
        return _soften_for_push_to_talk(
            CheckResult("mic capture", "fail", f"{cfg.mic_device}: {e}"), presence,
        )

@doctor_check(order=7, group="audio", label="tts output", needs_cfg=True)
def check_tts_open(cfg: Config) -> CheckResult:
    """Verify TTS output device is enumerable. Doesn't actually open the
    stream — opening + starting a `sd.RawOutputStream` against a dmix
    device races with the running jasper-voice (which holds a writer
    open) and yields false-negative "can't open" errors while TTS is
    working. `query_devices` is enough to confirm the device exists in
    PortAudio's enumeration and has output channels available."""
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
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as e:
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
    # The reconciler-emitted final-edge format (JASPER_OUTPUTD_DAC_FORMAT in
    # /var/lib/jasper/outputd.env, which env_load sources). Read, never
    # re-derived from the registry: the emitted value is what outputd and the
    # chip-AEC alignment identity actually see. Disclosure only — it prints one
    # value and detects no drift on its own; registry-vs-emission drift is
    # caught by tests/test_audio_hardware_reconcile.py. Unset/blank is the
    # S16_LE edge (an unrecognized DAC, or a box predating the emit).
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

    ``jasper-web`` runs non-root and writes active-speaker staged/commissioning
    configs and room-correction configs into this dir *atomically* (temp file
    in-dir + rename), which needs directory group-write. install.sh's intended
    posture is ``root:jasper 2775``; a deploy that lands it root-only (e.g. an
    interrupted install before the widen step) makes non-root staging fail with
    ``PermissionError`` and surfaces to the household as "could not load the
    silent active-speaker setup". Catch that here instead of at the wizard."""

    label = "CamillaDSP config dir writable"
    try:
        st = path.stat()
    except FileNotFoundError:
        return CheckResult(label, "warn", f"{path} missing — re-run install.sh")
    except OSError as exc:
        return CheckResult(label, "warn", f"{path}: {exc}")

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
        )
    return CheckResult(label, "ok", detail)


@doctor_check(order=20.6, group="audio")
def check_camilla_configs_writable() -> CheckResult:
    """Guard the CamillaDSP config dir's group-write posture for jasper-web."""

    return _camilla_configs_writable_result(CAMILLA_CONFIGS_DIR)


@doctor_check(order=20.7, group="audio")
def check_dac_usb_sync_mode() -> CheckResult:
    """Classify the speaker DAC's USB sync mode as an advisory clock-coherence
    observation for chip-AEC.

    This is NOT the chip-AEC gate: USB sync mode is *one* clock-coherence
    signal, while the binding production gate is the fixed DAC-profile
    qualification (`resolve_chip_aec_dac_gate` in jasper/chip_aec_policy.py).
    An async-but-approved DAC still passes that gate. Read this check as an
    observation that helps explain a chip-AEC verdict, never as an
    enable/disable switch.

    Chip-AEC assumes the speaker output and the mic reference share a clock
    domain. A USB Audio *playback* endpoint that is synchronous or adaptive
    (host-paced) keeps the DAC on the host clock the chip references; an
    *asynchronous* endpoint runs its own crystal and can drift against the
    mic.

    The endpoint sync tag is read once by the output-hardware reconciler from
    /proc/asound/card<N>/stream0 and persisted into
    OutputHardwareState.child_devices[*].endpoint_sync; this check only
    classifies it, against the *selected output DAC's* card (never the XVF
    mic's, which has its own stream0).

    Skip-if-not-applicable: with no XVF3800 mic present, chip-AEC is
    irrelevant and this reports 'skipped'. I2S/HAT DACs (no USB endpoint,
    clock slave on the I2S bus) report 'n/a — I2S' as OK.
    """
    if not xvf3800.is_present():
        return CheckResult(
            "DAC USB sync mode", "ok",
            "skipped — no XVF3800 mic present, chip-AEC not applicable",
        )

    state = _output_hardware_state_or_none()
    dac_id = _observed_output_dac_id(state)
    if state is None:
        return CheckResult(
            "DAC USB sync mode", "warn",
            "output hardware state unavailable — run "
            "`sudo systemctl start jasper-audio-hardware-reconcile`",
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
        )

    # I2S / HAT DAC: a known DAC profile with no USB endpoint sync tag — its
    # clock coherence is governed by the I2S frame clock, not a USB tag.
    if all(tag == "" for _card, tag in syncs):
        if dac_id not in {"", "unknown"}:
            return CheckResult(
                "DAC USB sync mode", "ok",
                f"n/a — {dac_id} is not a USB DAC (I2S clock slave); "
                "USB sync mode does not gate chip-AEC",
            )
        return CheckResult(
            "DAC USB sync mode", "warn",
            "no USB endpoint sync tag and DAC profile is unknown",
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
        )
    return CheckResult(
        "DAC USB sync mode", "ok",
        f"synchronous USB playback endpoint ({', '.join(coherent)}); "
        "clock-coherence observation only — production chip-AEC is gated by "
        f"fixed DAC-profile qualification (profile={dac_id})",
    )


@doctor_check(order=21, group="audio")
def check_apple_dongle_audio() -> CheckResult:
    """Apple's USB-C → 3.5mm Headphone Jack Adapter only exposes its
    USB Audio class interface when something is plugged into the analog
    3.5mm jack. With no analog load lsusb sees the chip but no audio card
    enumerates, so the reconciler's record names no DAC at all. Naming that
    state gives the operator a clear signal instead of a generic ALSA error.

    The record is read for OBSERVED hardware, not for whether the reconciler is
    driving it, so a full complement of cards is ``ok`` whatever the record's
    status.
    """
    state = _output_hardware_state_or_none()
    dac_id = _observed_output_dac_id(state)
    if dac_id != "unknown" and not _apple_output_profile_active(dac_id):
        return CheckResult(
            "Apple dongle", "ok",
            f"skipped — active output DAC is {dac_id}",
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
                "Apple dongle", "ok",
                "skipped — no active output DAC and no Apple dongle on USB",
            )
        return CheckResult(
            "Apple dongle", "warn",
            f"{usb_count} Apple USB-C adapter(s) on USB but the output-hardware "
            "record names no audio card for them. Plug speakers/headphones into "
            "the dongle's 3.5mm jack — the chip stays in low-power mode without "
            "an analog load — then run "
            "`sudo systemctl start jasper-audio-hardware-reconcile`.",
        )
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
    return CheckResult(
        "Apple dongle",
        "warn",
        f"USB present but only {len(cards)} Apple audio card(s) enumerated; "
        "check analog loads on the 3.5mm jack(s).",
    )

@doctor_check(order=22, group="audio", exclusive_group="audio-probe")
def check_dongle_headphone_at_max() -> CheckResult:
    """The Apple dongle's analog Headphone control should be pinned at
    100%. Anything lower throws away analog headroom that we'd rather
    have available to the digital chain — main_volume in CamillaDSP is
    the user-facing knob, the dongle is meant to be a pass-through
    ceiling.

    `jasper-dac-init.service` sets this on every boot; if it's drifted,
    this check catches it — a Headphone control left low (e.g. 40%,
    -36 dB) costs audible loudness with nothing else red."""
    state = _output_hardware_state_or_none()
    dac_id = _observed_output_dac_id(state)
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
    cards = _apple_dongle_cards_from_state(state)
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

from . import audio_runtime as audio_runtime
from .audio_runtime import (
    _FANIN_EXPECTED_ALOOP_INPUTS,
    _OUTPUTD_EXPECTED_DAC_PCM,
    _OUTPUTD_EXPECTED_DUAL_DAC_PCM,
    _OUTPUTD_STATUS_SOCKET,
    _asound_non_comment_text,
    _asound_pcm_block,
    check_aec_clock_drift,
    check_audio_runtime_plan,
    check_camilla_service,
    check_fanin_asound_wiring,
    check_fanin_binary_installed,
    check_fanin_coupling,
    check_fanin_service,
    check_fanin_tts_drops,
    check_fanin_ring_stall,
    check_outputd_service,
    check_ring_conf_floor_render,
    check_ring_geometry_coherence,
    check_ring_ioplug_provenance,
    check_ring_platform_assets,
)

__all__ = [
    "_FANIN_EXPECTED_ALOOP_INPUTS",
    "_OUTPUTD_EXPECTED_DAC_PCM",
    "_OUTPUTD_EXPECTED_DUAL_DAC_PCM",
    "_OUTPUTD_STATUS_SOCKET",
    "_asound_non_comment_text",
    "_asound_pcm_block",
    "check_aec_clock_drift",
    "check_audio_runtime_plan",
    "check_camilla_service",
    "check_fanin_asound_wiring",
    "check_fanin_binary_installed",
    "check_fanin_coupling",
    "check_fanin_service",
    "check_fanin_tts_drops",
    "check_fanin_ring_stall",
    "check_outputd_service",
    "check_ring_conf_floor_render",
    "check_ring_geometry_coherence",
    "check_ring_ioplug_provenance",
    "check_ring_platform_assets",
]

def _devices_volume_limit_from_text(text: str) -> float | None:
    """``devices.volume_limit`` from a CamillaDSP config, or None if absent /
    null. Uses the depth-aware shared devices parser so a nested capture or
    playback field cannot masquerade as the global fader ceiling."""
    value = parse_camilla_devices_config(text).get("volume_limit")
    if value is None:
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

@doctor_check(order=28.2, group="audio")
def check_camilla_ring_chunk_fits() -> CheckResult:
    """Verify a ring-crossing Camilla config asks for a chunk the ring can hold.

    CamillaDSP sets ``avail_min`` to its chunksize and ALSA refuses an
    ``avail_min`` above the device's buffer, so a config naming a ring PCM with
    a chunk over the ring's capacity does not degrade — CamillaDSP exits at open
    and systemd restart-loops it, and the speaker emits nothing at all.

    The emitters cannot land that config any more
    (``resolve_camilla_latency_for_devices`` clamps the resolved chunk), so this
    is the standing surface for the one case the clamp cannot reach: a config
    written by an OLDER build and still on disk, which has taken a box silent
    through repeated restarts while every other check stayed green.

    Removal condition: delete this check once no supported upgrade path can
    still carry a pre-clamp config onto a box.
    """
    label = "camilla ring chunk"
    statefile, config_path = _active_camilla_config_path()
    if config_path is None:
        return CheckResult(label, "warn", f"could not read config_path from {statefile}")
    path = Path(config_path)
    if not path.exists():
        return CheckResult(
            label, "fail", f"statefile points at missing config {config_path}"
        )
    try:
        devices = parse_camilla_devices_config(path.read_text())
    except (OSError, ValueError) as e:
        return CheckResult(label, "fail", f"could not read {config_path}: {e}")

    ring_ends = [
        name
        for name in (devices.get("capture_device"), devices.get("playback_device"))
        if name in RING_PCM_DEVICES
    ]
    chunksize = devices.get("chunksize")
    if not ring_ends or chunksize is None:
        return CheckResult(
            label, "ok",
            f"{config_path} names no ring end (chunksize={chunksize})",
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
        )
    # Say so when the clamp is what put this number here; otherwise the box runs
    # a chunk its own DacProfile does not declare with no on-box explanation.
    # Asked of the SAME resolver the emitters fall back to, never of a second
    # derivation of "which DAC is active" — a disclosure that re-derived it
    # could name a floor no emitter used.
    unclamped = resolve_camilla_chunksize()
    clamped = (
        f", clamped from the {unclamped} this box resolves to"
        if unclamped > capacity
        else ""
    )
    return CheckResult(
        label, "ok",
        f"chunksize={chunksize} fits the ring's {capacity}-frame capacity "
        f"({'/'.join(ring_ends)}){clamped}",
    )


@doctor_check(order=28.5, group="audio")
def check_active_speaker_runtime_graph() -> CheckResult:
    """Report the graph selected for saved speaker intent, fail closed if unsafe."""
    from jasper.active_speaker.runtime_contract import (
        CONTRACT_UNCONFIGURED,
        GRAPH_PARKED_ALL_MUTED,
        classify_bass_extension_graph,
        classify_output_contract,
        parked_muted_exits,
        topology_allows_flat_dac_graph,
    )
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
    # The SSOT that authorizes a flat DAC graph is deliberately narrower than
    # "not roleful": unconfigured and incomplete/invalid non-roleful layouts
    # are not passive playback contracts.
    if topology_allows_flat_dac_graph(contract):
        return CheckResult(
            "active speaker runtime graph",
            "ok",
            f"{contract.classification}: explicit passive layout is valid",
        )

    statefile, config_path = _active_camilla_config_path()
    if config_path is None:
        return CheckResult(
            "active speaker runtime graph",
            "fail",
            (
                f"could not read config_path from {statefile}; saved topology "
                "does not permit an unchecked flat fallback"
            ),
        )
    path = Path(config_path)
    if not path.exists():
        return CheckResult(
            "active speaker runtime graph",
            "fail",
            f"statefile points at missing config {config_path}",
        )
    from ...active_speaker.baseline_profile import baseline_profile_state_path
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
    if graph.classification == GRAPH_PARKED_ALL_MUTED and graph.allowed:
        # A parked graph is intentional silence, not a broken runtime — both a
        # zero-group topology (the household must choose a layout) and an
        # incomplete roleful layout. The action is owned by
        # ``parked_muted_exits`` so doctor, /state, and the dashboard cannot
        # invent three versions of it.
        if contract.classification == CONTRACT_UNCONFIGURED or (
            contract.requires_roleful_graph
        ):
            return CheckResult(
                "active speaker runtime graph",
                "warn",
                (
                    f"parked silent for {contract.classification}: "
                    f"{parked_muted_exits(topology)}"
                ),
                speaker_silent=True,
            )
        detail = (
            contract.issues[0]["message"]
            if contract.issues
            else "saved layout is not a complete passive mono or stereo layout"
        )
        return CheckResult("active speaker runtime graph", "fail", detail)
    if graph.allowed:
        if contract.classification == CONTRACT_UNCONFIGURED:
            return CheckResult(
                "active speaker runtime graph",
                "fail",
                "unconfigured topology must use the proved parked graph",
            )
        if not contract.requires_roleful_graph:
            detail = (
                contract.issues[0]["message"]
                if contract.issues
                else "saved layout is not a complete passive mono or stereo layout"
            )
            return CheckResult("active speaker runtime graph", "fail", detail)
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


@doctor_check(order=28.6, group="audio")
def check_active_speaker_topology_blockers() -> CheckResult:
    """Name the saved layout's unresolved blockers on a parked speaker.

    A blocker on a roleful topology no longer aborts the deploy (#2145): the
    parked graph is structurally silent (File sink, every output hard-muted),
    so a blocker that cannot make it unsafe no longer refuses it. This check is
    the replacement signal at the household's own diagnostic surface.

    The blockers and the way OUT of parked are stated as two facts, because
    they are two: parking is gated on the absence of a staged startup graph, not
    on the blockers, so clearing them does not on its own restore sound. The
    exits come from :func:`parked_muted_exits`, the owned capability-aware
    helper the CLI and `/state` also use — it drops "finish crossover preview"
    on a DAC that has no active outputd lane, where that action can never
    succeed.

    WARN, never FAIL: a parked speaker is silent, not broken — the state is
    "commissioning is unfinished". `jasper-doctor` exits non-zero only on fails,
    so warning keeps a mid-commission box deployable (#2145).

    Scoped to the parked outcome on purpose: a blocker-bearing topology that
    DOES have a staged graph still fails the deploy and is reported by
    `check_active_speaker_runtime_graph`.
    """

    from jasper.active_speaker.runtime_contract import (
        PARKED_MUTED_STATUS,
        classify_output_contract,
        parked_muted_exits,
        safe_graph_for_current_topology,
    )
    from jasper.output_topology import OutputTopologyError, load_output_topology_strict

    name = "active speaker topology blockers"
    try:
        topology = load_output_topology_strict()
    except OutputTopologyError:
        # Points, does not restate: `check_active_speaker_runtime_graph` and
        # `check_active_speaker_output_hardware_match` both already print the
        # parse error verbatim. Still a `fail`, not an ok-defer — this check
        # cannot answer its question without a topology, and a green line next
        # to two reds would read as "the layout is fine".
        return CheckResult(
            name,
            "fail",
            (
                "saved output topology could not be loaded, so its blockers "
                "cannot be listed; the active speaker runtime graph check "
                "reports the parse error"
            ),
        )

    contract = classify_output_contract(topology)
    if not contract.requires_roleful_graph:
        return CheckResult(
            name,
            "ok",
            f"{contract.classification}: no roleful/protected outputs configured",
        )
    if not contract.issues:
        return CheckResult(
            name, "ok", f"{contract.classification}: no topology blockers"
        )

    codes = ",".join(str(issue.get("code") or "") for issue in contract.issues)
    messages = "; ".join(
        str(issue.get("message") or "")
        for issue in contract.issues
        if issue.get("message")
    )
    try:
        decision = safe_graph_for_current_topology(topology)
    except (OSError, ValueError, OutputTopologyError) as exc:
        # The blockers are the finding; a failed selection probe must not hide
        # them, so report what is known and say the probe did not answer.
        return CheckResult(
            name,
            "warn",
            (
                f"saved layout has unresolved blockers={codes}"
                f"{': ' + messages if messages else ''}; "
                f"could not determine the selected runtime graph ({exc}). "
                "Fix the speaker layout at http://<speaker>/sound/setup/"
            ),
        )

    if decision.status != PARKED_MUTED_STATUS:
        return CheckResult(
            name,
            "ok",
            (
                f"saved layout has blockers={codes}, but the speaker is not "
                f"parked (runtime graph: {decision.status}); reported by the "
                "active speaker runtime graph check"
            ),
        )

    return CheckResult(
        name,
        "warn",
        (
            f"speaker is parked silent and its saved layout still has "
            f"unresolved blockers={codes}"
            f"{': ' + messages if messages else ''}. "
            "Clearing them does not by itself unpark the speaker — "
            f"next: {parked_muted_exits(topology)}. "
            "Fix the layout at http://<speaker>/sound/setup/"
        ),
        # Reached only when `safe_graph_for_current_topology` returned
        # PARKED_MUTED_STATUS, so the speaker is provably emitting nothing
        # (#2471). The two warn branches above are NOT silent.
        speaker_silent=True,
    )


def _sound_profile_path() -> Path:
    return Path(
        os.environ.get(
            "JASPER_SOUND_PROFILE_PATH",
            "/var/lib/jasper/sound_profile.json",
        )
    )

@doctor_check(order=30, group="audio")
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

@doctor_check(order=30.5, group="audio")
def check_bass_extension_profile() -> CheckResult:
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )
    from jasper.bass_extension.profile import evaluate_bass_extension_profile
    from jasper.output_topology import load_output_topology

    evaluation = evaluate_bass_extension_profile(
        topology=load_output_topology(),
        applied_baseline_state=load_applied_baseline_profile_state(),
    )
    if evaluation.status == "missing":
        return CheckResult(
            "bass extension profile", "ok", "bass extension: not commissioned"
        )
    if evaluation.status == "malformed":
        return CheckResult(
            "bass extension profile",
            "fail",
            f"bass extension profile is malformed: {evaluation.detail}",
        )
    if evaluation.status == "stale":
        refusals = ",".join(refusal.value for refusal in evaluation.refusals)
        return CheckResult(
            "bass extension profile",
            "warn",
            f"bass extension profile is stale [{refusals}]: {evaluation.detail}",
        )
    if evaluation.status == "bypassed":
        return CheckResult(
            "bass extension profile", "ok", "bass extension profile is bypassed"
        )
    assert evaluation.profile is not None
    return CheckResult(
        "bass extension profile",
        "ok",
        f"accepted; deepest={evaluation.profile.targets[0].fp_hz:g}Hz "
        f"natural={evaluation.profile.targets[-1].fp_hz:g}Hz",
    )

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

@doctor_check(order=31.5, group="audio")
def check_active_speaker_baseline_canonical() -> CheckResult:
    """Canonical ``active_speaker_baseline.yml`` durability (issue #1666).

    ``build_baseline_profile_candidate`` never writes the canonical
    ``baseline_config_path()`` name directly; every apply/restore promotes
    the just-applied candidate's bytes onto it as a best-effort, fail-soft
    copy after CamillaDSP already confirmed the candidate live. A promote
    failure (disk full, permissions drift) leaves that copy stale without
    affecting the audible graph — CamillaDSP's statefile self-persists the
    running candidate path independently. This check surfaces that gap for the
    other readers who trust the canonical name (the multiroom follower
    fallback, operators, this doctor). WARN, never FAIL: the live graph is the
    audible truth and is correct either way.
    """
    from jasper.active_speaker.baseline_profile import (
        active_layer_a_fingerprint,
        baseline_config_path,
    )
    from jasper.active_speaker.profile import ActiveSpeakerConfigError

    label = "active speaker baseline canonical"
    statefile, live_path_raw = _active_camilla_config_path()
    if live_path_raw is None:
        # A missing/unreadable outputd statefile is already a real failure at
        # the checks that own it (check_active_speaker_runtime_graph fails when
        # a roleful topology needs it). This check's scope is only "does
        # canonical mirror the live baseline", which cannot be evaluated here —
        # not applicable, not a warning.
        return CheckResult(
            label, "ok",
            f"could not read config_path from {statefile}; canonical-file "
            "check not applicable",
        )
    live_path = Path(live_path_raw)
    canonical = baseline_config_path()
    if live_path == canonical:
        return CheckResult(
            label, "ok", f"live config is the canonical file ({canonical})",
        )
    if not _is_baseline_candidate_sibling(live_path, canonical):
        return CheckResult(
            label, "ok",
            f"live config ({live_path}) is not an active-speaker baseline "
            "candidate; canonical-file check not applicable",
        )
    if not canonical.exists():
        return CheckResult(
            label, "warn",
            f"canonical baseline file is missing ({canonical}) while the live "
            f"config is an applied baseline candidate ({live_path}); the next "
            "apply or restore re-promotes it",
        )
    if not live_path.exists():
        return CheckResult(
            label, "warn",
            f"live baseline candidate file is missing on disk ({live_path}); "
            f"cannot compare it against canonical ({canonical})",
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
        )
    if live_fingerprint == canonical_fingerprint:
        return CheckResult(
            label, "ok",
            f"canonical file ({canonical}) matches the live applied baseline "
            f"({live_path})",
        )
    return CheckResult(
        label, "warn",
        f"canonical baseline file ({canonical}) does not match the live "
        f"applied config ({live_path}); the running graph is correct, but the "
        "canonical file is stale for other readers (multiroom follower "
        "fallback, operators)",
    )


@doctor_check(order=31.55, group="audio", label="active speaker applied graph")
def check_active_speaker_applied_graph() -> CheckResult:
    """Is the durable graph the one the applied profile names?

    A crossover-v2 round that ends on a verify rejection banks no adoption and
    auto-restores nothing, but its apply has already repointed CamillaDSP's
    persisted ``config_file_path`` at the rejected candidate — leaving the
    anchor's per-driver values (delays, gains) disagreeing with the applied
    profile's with no surface naming it. ``setup_status`` already binds the
    two; this reads the binding out with the values.

    **Compared at the DURABLE anchor, never at the running graph** — with no
    readback argument the reader resolves its path from the CamillaDSP
    statefile. Every runtime-only swap (audition, ADR-0193; the measurement
    session graph; the per-driver commissioning load) installs through
    ``set_active_config_raw``, which leaves that path and its bytes alone, so
    none can read as drift here. A staged/commissioning anchor IS a durable
    repoint, so it is excluded by name instead.

    WARN, never FAIL, and no new gate: the anchor is the audible truth either
    way, and the choice of remedy is the operator's.
    """

    from ...active_speaker.setup_status import (
        IN_SEQUENCE_CAPTURE_ANCHOR_REASON,
        read_active_speaker_setup_status,
    )

    label = "active speaker applied graph"
    try:
        status = read_active_speaker_setup_status()
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return CheckResult(label, "warn", f"could not read speaker setup: {exc}")
    protected = status.get("protected_profile")
    binding = protected.get("layer_a_binding") if isinstance(protected, dict) else None
    if not isinstance(binding, dict):
        return CheckResult(label, "ok", "no applied active-speaker profile to bind")
    issues = status.get("issues")
    if any(
        isinstance(issue, dict)
        and issue.get("code") == IN_SEQUENCE_CAPTURE_ANCHOR_REASON
        for issue in (issues if isinstance(issues, list) else [])
    ):
        return CheckResult(
            label, "ok",
            "a commissioning/staged graph is the durable anchor by design; "
            "applied-profile binding not applicable",
        )
    if binding.get("matches") is True:
        return CheckResult(
            label, "ok",
            "the durable graph is the one the applied profile names "
            f"(layer_a={binding.get('loaded_fingerprint')})",
        )
    if binding.get("status") != "mismatch":
        return CheckResult(
            label, "ok",
            "applied-profile graph binding not evaluated "
            f"({binding.get('status') or 'absent'})",
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
    )


@doctor_check(order=31.6, group="audio", label="active speaker startup hold")
def check_active_speaker_startup_hold() -> CheckResult:
    """A staged-startup hold marker with no startup load behind it is stale.

    ``load_protected_startup_config`` takes an ephemeral ``/run`` marker before
    it applies the all-muted staged anchor, and while that marker is present
    ``safe_graph_for_current_topology`` preserves the anchor instead of
    restoring the saved baseline (``jasper.active_speaker.startup_hold``). A
    marker left behind after the load it belonged to went away therefore keeps a
    commissioned box on its SILENT anchor across the next reconcile — recoverable
    (the marker is in ``/run``, so a reboot clears it, and a rollback clears it
    sooner) but invisible without this line.

    This is the surface the household-facing "Open System status" copy for
    ``staged_startup_hold_unavailable`` points at, so it has to be able to say
    something. WARN, never FAIL: preserving an all-muted anchor is the safe
    direction — silent, never loud — and the load path's own blocker is what
    fails closed.
    """

    from ...active_speaker.startup_hold import (
        staged_startup_hold_active,
        startup_hold_marker_path,
    )
    from ...active_speaker.startup_load import load_startup_load_state

    label = "active speaker startup hold"
    marker = startup_hold_marker_path()
    if not staged_startup_hold_active():
        return CheckResult(label, "ok", f"no staged-startup hold in flight ({marker})")
    status = str(load_startup_load_state().get("status") or "unknown")
    if status == "loaded":
        return CheckResult(
            label, "ok",
            f"staged-startup hold held by an in-flight protected load ({marker})",
        )
    return CheckResult(
        label, "warn",
        f"stale staged-startup hold at {marker}: the startup load is "
        f"'{status}', not 'loaded', so no commission is in flight — the graph "
        "selector keeps preserving the silent all-muted anchor instead of "
        "restoring the saved baseline. Roll back the startup load from "
        "http://jts.local/sound/ or reboot to clear it (/run is tmpfs).",
    )


@doctor_check(order=31.7, group="audio", label="room correction authority")
def check_room_correction_authority() -> CheckResult:
    """Room correction runs unproven — this is the line that says so.

    Ruling S10 and ADR-0019: an unminted, stale or unreadable commissioning
    receipt no longer refuses a room-correction run. The run proceeds on the
    applied crossover and simply does not bank a verified result, which means
    the only place a household can learn the difference is here. Never FAIL:
    nothing is broken, and nothing is stopped. The denials do not share one
    line, because they do not share a remedy — see ADR-0196.
    """

    from ...active_speaker._common import (
        ROOM_AUTHORITY_RECEIPT_ABSENT,
        ROOM_AUTHORITY_RECEIPT_UNREADABLE,
    )
    from ...active_speaker.setup_status import read_active_speaker_setup_status

    label = "room correction authority"
    try:
        status = read_active_speaker_setup_status()
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return CheckResult(label, "warn", f"could not read speaker setup: {exc}")
    acoustic = status.get("acoustic_commissioning")
    if not isinstance(acoustic, dict):
        return CheckResult(label, "warn", "speaker setup published no room decision")
    if acoustic.get("required") is not True:
        return CheckResult(label, "ok", "room correction needs no speaker authority")
    if acoustic.get("allowed") is True:
        return CheckResult(
            label, "ok",
            f"room correction is banked under {acoustic.get('authority')}",
        )
    reason = str(acoustic.get("reason") or "")
    detail = str(acoustic.get("detail") or "")
    cause = str(acoustic.get("cause") or "")
    if reason == ROOM_AUTHORITY_RECEIPT_ABSENT:
        # The state every uncommissioned speaker is in, which is most of them —
        # hence `ok`. But ABSENT is the module's catch-all default reason, so it
        # also covers a receipt that VANISHED under a verified lifecycle, a
        # genuine anomaly a bare "ok" would hide. Forwarding `cause` keeps that
        # sub-state visible (a store code vs "lifecycle is not verified")
        # without turning it into a nag.
        return CheckResult(
            label, "ok",
            f"room correction runs unbanked ({reason})"
            + (f": {cause}" if cause else ""),
        )
    if reason == ROOM_AUTHORITY_RECEIPT_UNREADABLE:
        # A machine fault, not a verdict on the record: the file and errno are
        # the sentence that ends the incident. Without them an operator reads
        # "unproven" and goes looking for a mint that was never the problem.
        return CheckResult(
            label, "warn",
            "room correction cannot read its commissioning record "
            f"({acoustic.get('cause') or reason}): {detail}",
        )
    return CheckResult(
        label, "warn", f"room correction runs unproven ({reason}): {detail}"
    )


@doctor_check(order=31.8, group="audio", label="active speaker setup notices")
def check_active_speaker_setup_notices() -> CheckResult:
    """The standing home for setup facts that no longer stop anything.

    Ruling S10 and ADR-0019 turn staleness and unproven-ness into loud
    disclosures rather than blocks — a topology fingerprint that rotated on a
    metadata edit being the worked example. Nothing else renders a non-blocker
    setup issue, so without this line the demotion would be a silent one.
    Blockers keep their own surfaces (`/state`, the landing page, the volume
    and grouping refusals) and are deliberately not repeated here.
    """

    from ...active_speaker.setup_status import read_active_speaker_setup_status

    label = "active speaker setup notices"
    try:
        status = read_active_speaker_setup_status()
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return CheckResult(label, "warn", f"could not read speaker setup: {exc}")
    issues = status.get("issues")
    notices = [
        issue for issue in (issues if isinstance(issues, list) else [])
        if isinstance(issue, dict) and issue.get("severity") != "blocker"
    ]
    if not notices:
        return CheckResult(label, "ok", "no standing speaker setup notices")
    return CheckResult(
        label, "warn",
        "; ".join(
            f"{issue.get('code')}: {issue.get('message')}" for issue in notices
        ),
    )
