# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — audio domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from ...audio_hardware.dac import (
    APPLE_USB_C_DONGLE_ID,
    mixer_control_groups_for as _dac_mixer_control_groups_for,
)
from ...camilla import CamillaController, CamillaUnavailable
from ...camilla_config_contract import (
    DEFAULT_VOLUME_LIMIT_DB,
    parse_camilla_devices_config,
)
from ...config import Config
from ...env_load import parse_env_file
from ... import ring_assets
from ...mics import xvf3800
from ...output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    OutputHardwareState,
    load_state as _load_output_hardware_state,
)
from ...mic_presence import read_mic_presence
from ._registry import doctor_check
from ._shared import (
    CheckResult,
    _active_audio_dac_env,
    _parked_as_bonded_follower,
    _active_audio_dac_id,
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

# --- jts_ring platform assets (audio-graph consolidation P1) ---
# The ALSA plugin dir ALSA actually dlopen()s ioplugs from on aarch64
# Trixie (verified live on jts.local/jts3; bluealsa/jack register plugins
# here too). Kept in lockstep with JTS_RING_ALSA_PLUGIN_DIR in
# deploy/lib/install/ring-platform.sh. NOTE: this is a hardcoded aarch64
# multiarch path in two places (here + ring-platform.sh's env-overridable
# JTS_RING_ALSA_PLUGIN_DIR). Fine for the mandated 64-bit fleet
# (BRINGUP/QUICKSTART pin RPiOS Lite 64-bit); if the installer dir is ever
# overridden for another arch, this constant would need to move with it
# (deriving both from `dpkg-architecture -qDEB_HOST_MULTIARCH` would remove
# the assumption).
# Asset paths live in the shared jasper.ring_assets SSOT so the doctor probe and
# the coupling reconciler's activation gate name the same files. Re-exported here
# under the historical private names so the rest of this module (and its tests)
# stay stable.
_JTS_RING_ALSA_PLUGIN_DIR = ring_assets.RING_ALSA_PLUGIN_DIR
_JTS_RING_IOPLUG_SO = ring_assets.RING_IOPLUG_SO
_JTS_RING_CONF_D = ring_assets.RING_CONF_D
# The tmpfs directory the ring files live in (shipped by
# deploy/tmpfiles/jts-ring.conf). Module constant so tests can repoint it.
_JTS_RING_SHM_DIR = ring_assets.RING_SHM_DIR
# The two inert PCM names the conf.d defines, each paired with (probe tool,
# ring-file basename). The open probe against these both resolves the name
# AND forces ALSA to dlopen the ioplug .so; with no ring present it exercises
# the writer-dead / no-reader silence path, which terminates safely (the lab
# ring-proto resolvability step relies on this).
#
# The ring-file basename matters because the ioplug's open path is
# create-or-attach (O_RDWR|O_CREAT|O_EXCL in jts_ring_reader_open /
# jts_ring_writer_open): probing an ABSENT ring CREATES the file. That would
# violate P1's inertness invariant ("no ring file exists until P2 arms") and
# poison P2's first arm (a valid-magic ring with the conf.d placeholder
# geometry is a fail-closed open error, not a reclaimable magic-less file).
# The probe therefore snapshots each ring path's existence and unlinks only
# what it created — see _jts_ring_pcm_resolves. Basenames match
# deploy/alsa/conf.d/60-jts-ring.conf's `path` values (capture -> program.ring
# via the reader; playback -> content.ring via the writer).
_JTS_RING_PCMS = (
    ("jts_ring_capture", "arecord", "program.ring"),
    ("jts_ring_playback", "aplay", "content.ring"),
)


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

@doctor_check(order=3.5, group="audio", label="microphone")
def check_microphone() -> CheckResult:
    """Single headline for microphone presence — the one flag for "is there
    a mic?".

    Reads the reconciler's one canonical record via
    ``jasper.mic_presence.read_mic_presence`` and states present/absent + *why*
    in a single line. The downstream ``mic ALSA card`` / ``mic capture`` checks
    and the audio-open-failure log all defer to this same verdict instead of
    independently re-probing ALSA, so a missing mic is one yellow advisory —
    not a scatter of contradicting red failures. Absent is ``warn`` (never
    ``fail``): the reconciler parked voice and it auto-starts when a mic is
    reconnected, so it's noteworthy, not broken."""
    mp = read_mic_presence()
    status = "warn" if mp.absent_confirmed else "ok"
    return CheckResult("microphone", status, mp.summary)


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
    # No usable mic: the reconciler's single source of truth already
    # classified this and parked voice. Defer to the `microphone` headline —
    # independently re-probing `arecord -L` here only to report a red FAILURE
    # for an expected, auto-recovering state was the exact contradiction this
    # check used to create. See jasper/mic_presence.py.
    if read_mic_presence().absent_confirmed:
        return CheckResult(
            "mic ALSA card", "ok",
            "no microphone present — see the `microphone` check "
            "(voice parked, auto-starts when a mic is reconnected)",
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
    # Intentionally idle, not broken: the reconciler's single source of truth
    # confirms no usable mic and parked jasper-voice. Defer to the `microphone`
    # headline so a mic-less box / a unit mid-unplug is one advisory, not a red
    # line. A genuine open failure (no absent verdict but the device won't open
    # — custom or busy mic) still falls through to the probe + its fail below.
    # See jasper/mic_presence.py and docs/HANDOFF-hotplug-resilience.md "Layer 3".
    if read_mic_presence().absent_confirmed:
        return CheckResult(
            "mic capture", "ok",
            "no microphone present (expected) — see the `microphone` check; "
            "voice auto-starts when a mic is reconnected",
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
    except (ImportError, OSError, RuntimeError, ValueError) as e:
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
    except (OSError, ValueError, TypeError):
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


CAMILLA_CONFIGS_DIR = Path("/var/lib/camilladsp/configs")


def _camilla_configs_writable_result(
    path: Path, *, expected_group: str = "jasper"
) -> CheckResult:
    """CheckResult for the CamillaDSP config dir's group-write posture.

    ``jasper-web`` runs non-root (WS1 privilege drop) and writes active-speaker
    staged/commissioning configs and room-correction configs into this dir
    *atomically* (temp file in-dir + rename), which needs directory group-write.
    install.sh's intended posture is ``root:jasper 2775``; a deploy that lands it
    root-only (e.g. an interrupted install before the widen step) makes non-root
    staging fail with ``PermissionError`` and surfaces to the household as
    "could not load the silent active-speaker setup" (the jts3 2026-07-06
    incident). Catch that here instead of at the wizard.
    """

    import grp

    label = "CamillaDSP config dir writable"
    try:
        st = path.stat()
    except FileNotFoundError:
        return CheckResult(label, "warn", f"{path} missing — re-run install.sh")
    except OSError as exc:
        return CheckResult(label, "warn", f"{path}: {exc}")

    try:
        group_name = grp.getgrgid(st.st_gid).gr_name
    except (KeyError, OSError):
        group_name = str(st.st_gid)
    mode = st.st_mode & 0o7777
    group_writable = bool(st.st_mode & 0o0020)  # S_IWGRP
    detail = f"{path} mode={mode:04o} group={group_name}"
    if group_name != expected_group or not group_writable:
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
    observation for chip-AEC (Stage 6 of the audio-latency foundation work).

    This is NOT the chip-AEC gate. USB sync mode is *one* clock-coherence
    signal; the binding chip-AEC gate is DAC-profile qualification plus the
    outputd SRO clock verdict (`resolve_chip_aec_dac_gate` in
    jasper/chip_aec_policy.py), which never reads endpoint_sync. A
    synchronous/adaptive endpoint and an approved DAC happen to agree on
    today's Apple dongle, but that agreement is incidental — an
    async-but-approved DAC would still pass the binding gate. Read this check
    as a clock-coherence observation that helps explain a chip-AEC verdict,
    never as an enable/disable switch.

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
    dac_id = _effective_output_dac_id(state)
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
        # but the binding chip-AEC gate is DAC qualification + the outputd SRO
        # verdict (resolve_chip_aec_dac_gate), not this tag. WARN so a
        # maintainer notices the drift risk; software AEC3 keeps echo cancelled
        # either way.
        return CheckResult(
            "DAC USB sync mode", "warn",
            "async USB playback endpoint — weak clock coherence; chip-AEC is "
            "still gated by DAC qualification + the outputd SRO verdict "
            f"(async on {','.join(async_cards)}; profile={dac_id})",
        )
    return CheckResult(
        "DAC USB sync mode", "ok",
        f"synchronous USB playback endpoint ({', '.join(coherent)}); "
        "clock-coherence observation only — the binding chip-AEC gate is "
        f"DAC qualification + the outputd SRO verdict (profile={dac_id})",
    )


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

_FANIN_STATUS_SOCKET = "/run/jasper-fanin/control.sock"

_OUTPUTD_STATUS_SOCKET = "/run/jasper-outputd/control.sock"

_STATUS_RESPONSE_MAX_BYTES = 1_048_576


def _read_status_socket_bytes(socket_path: str, *, timeout: float) -> bytes:
    """Return the raw reply from a local JTS ``STATUS\n`` control socket.

    This helper owns only the shared socket lifecycle.  Callers deliberately
    retain their own retry, UTF-8/JSON parsing, and fail-versus-skip policy.
    """

    deadline = time.monotonic() + timeout

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        def set_remaining_timeout() -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("STATUS response deadline exceeded")
            sock.settimeout(remaining)

        set_remaining_timeout()
        sock.connect(socket_path)
        set_remaining_timeout()
        sock.sendall(b"STATUS\n")
        chunks: list[bytes] = []
        received = 0
        while True:
            set_remaining_timeout()
            chunk = sock.recv(65536)
            if not chunk:
                break
            received += len(chunk)
            if received > _STATUS_RESPONSE_MAX_BYTES:
                raise OSError("STATUS response exceeds byte limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _read_status_socket(socket_path: str) -> dict[str, object]:
    payload = _read_status_socket_bytes(socket_path, timeout=1.0).decode("utf-8")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("STATUS response root is not an object")
    return parsed


def _route_live_state_issues_for_doctor(plan: object) -> tuple[str, ...]:
    from jasper.audio_validation import route_live_state_issues

    identity = plan.route_latency_identity()
    issues: list[str] = []
    usbsink_state: dict[str, object] | None = None
    fanin_status: dict[str, object] | None = None

    try:
        parsed = json.loads(Path("/run/jasper-usbsink/state.json").read_text())
        if isinstance(parsed, dict):
            usbsink_state = parsed
        else:
            issues.append("live_usbsink_state_malformed")
    except (OSError, json.JSONDecodeError) as e:
        issues.append(f"live_usbsink_state_unreadable:{type(e).__name__}")

    try:
        fanin_status = _read_status_socket(_FANIN_STATUS_SOCKET)
    except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as e:
        issues.append(f"live_fanin_status_unreadable:{type(e).__name__}")

    return tuple(
        dict.fromkeys(
            (
                *issues,
                *route_live_state_issues(
                    identity,
                    usbsink_state=usbsink_state,
                    fanin_status=fanin_status,
                ),
            )
        )
    )


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


# outputd STATUS reports, per content/dac section, an all-time xrun_count plus
# two rolling fields the daemon already computes: xrun_rate_per_hour (count /
# uptime-hours) and last_xrun_age_ms (ms since the most recent xrun, null when
# none). The doctor WARN keys on BOTH so it flags a *sustained, current*
# problem — a high rate alone could be a long-ago burst diluting slowly as
# uptime grows, and a recent single xrun alone is a normal transient. Only a
# rate that's still meaningfully high AND a recent xrun is worth a yellow line.
_OUTPUTD_XRUN_RATE_WARN_PER_HOUR = 6.0
_OUTPUTD_XRUN_RECENT_AGE_MS = 300_000  # 5 minutes


def _outputd_xrun_rate_warning(
    content: dict[str, object],
    dac: dict[str, object],
) -> str | None:
    """Return a one-clause WARN reason when either outputd lane shows a
    sustained xrun rate with a recent xrun, else None.

    Keyed on the daemon-computed ``xrun_rate_per_hour`` and ``last_xrun_age_ms``
    so a burst that has since cleared (recent-but-low-rate, or high-rate but
    stale) does NOT warn. Both sections are checked independently; the worst
    qualifying lane is reported.
    """

    def _f(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    worst: tuple[float, str] | None = None
    for label, section in (("content", content), ("dac", dac)):
        if not isinstance(section, dict):
            continue
        rate = _f(section.get("xrun_rate_per_hour"))
        age = _f(section.get("last_xrun_age_ms"))  # null → None → no recent xrun
        if rate is None or age is None:
            continue
        if rate >= _OUTPUTD_XRUN_RATE_WARN_PER_HOUR and age <= _OUTPUTD_XRUN_RECENT_AGE_MS:
            reason = (
                f"{label} xrun_rate_per_hour={rate:.1f} "
                f"(last_xrun_age_ms={int(age)})"
            )
            if worst is None or rate > worst[0]:
                worst = (rate, reason)
    return worst[1] if worst else None


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

    # No usbsink_substream write alias: USB audio is DIRECT-captured by jasper-fanin
    # from hw:UAC2Gadget (the aloop solo bridge that wrote hw:Loopback,0,3 was
    # removed 2026-07-10). fan-in still READS the pair-3 capture side as the usbsink
    # lane's idle fallback — see _FANIN_EXPECTED_INPUTS above — but nothing writes it.
    expected_aliases = {
        "librespot_substream": "hw:Loopback,0,0",
        "shairport_substream": "hw:Loopback,0,1",
        "bluealsa_substream": "hw:Loopback,0,2",
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
        try:
            payload = _read_status_socket_bytes(socket_path, timeout=2.0)
            break
        except OSError as e:
            last_error = e
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

    body = payload.decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but UDS STATUS returned invalid JSON: {e}",
        )
    if not isinstance(data, dict):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but UDS STATUS root is {type(data).__name__}, expected object",
        )

    output = data.get("output", {})
    if not isinstance(output, dict):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS response missing output{}",
        )
    from jasper.fanin.coupling_reconcile import read_persisted_coupling
    from jasper.fanin_coupling import COUPLING_SHM_RING

    coupling = read_persisted_coupling()
    # The fan-in STATUS echoes its live coupling transport (state.rs
    # push_kv_str("transport", ...)): "loopback" (default) or "shm_ring" (Ring A —
    # with a "ring" observability block). Expect the STATUS to match the persisted
    # intent so a coupling that failed to restart onto the box is caught. shm_ring
    # keeps a lossy aloop MIRROR on lane 7, so output.pcm still reports
    # hw:Loopback,0,7 (checked below, unchanged); only the transport string and the
    # ring block differ from loopback.
    expected_transport = {
        COUPLING_SHM_RING: "shm_ring",
    }.get(coupling, "loopback")
    actual_transport = output.get("transport")
    if actual_transport != expected_transport:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but STATUS output.transport={actual_transport!r}; "
            f"expected {expected_transport!r} for persisted coupling={coupling!r}. "
            "Run jasper-fanin-coupling-reconcile to restart fan-in onto the "
            "persisted topology.",
        )
    if coupling == COUPLING_SHM_RING:
        ring = output.get("ring")
        if not isinstance(ring, dict):
            return CheckResult(
                "jasper-fanin service",
                "fail",
                "active but shm_ring STATUS is missing output.ring metrics — "
                "fan-in is not actually writing Ring A. Run "
                "jasper-fanin-coupling-reconcile shm_ring.",
            )

    output_pcm = output.get("pcm")
    # Both loopback and shm_ring (via its lane-7 aloop mirror) report
    # hw:Loopback,0,7 as output.pcm.
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
    frames = output.get("frames_written", 0)
    xruns = output.get("xrun_count", 0)
    input_buffer_frames = data.get("input_buffer_frames")
    output_buffer_frames = output.get("buffer_frames")
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
    if output_buffer_frames < 1024:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active, but runtime output_buffer_frames={output_buffer_frames} is below "
            f"1024. The 1024-frame fan-in output queue is the production "
            f"floor validated on the low-latency Camilla path; lower values "
            f"need fresh hardware validation before shipping. Check "
            f"/var/lib/jasper/fanin.env and "
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
        f"transport={actual_transport}, "
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
        payload = _read_status_socket_bytes(socket_path, timeout=2.0)
        data = json.loads(payload.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            raise ValueError(
                f"STATUS response root is {type(data).__name__}, not object"
            )
    except (OSError, json.JSONDecodeError, ValueError) as e:
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


def _loaded_device_field(config_path: Path, block: str, field: str) -> str | None:
    """A field from ``devices.<block>`` in a CamillaDSP config, or None.

    Tiny indent-aware scan (no YAML dep): find the 2-space device block, return
    its first 4-space ``field:`` value. Quotes are stripped for path fields.
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    target_block = f"{block}:"
    target_field = f"{field}:"
    in_block = False
    for raw in text.splitlines():
        is_2space = raw.startswith("  ") and not raw.startswith("   ")
        if is_2space and raw.strip() == target_block:
            in_block = True
            continue
        if in_block:
            if raw.startswith("    ") and raw.strip().startswith(target_field):
                return raw.split(":", 1)[1].strip().strip("\"'")
            # A sibling 2-space key (playback:/resampler:/...) or any dedent ends
            # the block — never read a sibling block's field.
            if is_2space or (raw[:1] not in (" ", "") and raw.strip()):
                in_block = False
    return None


def _loaded_capture_type(config_path: Path) -> str | None:
    """The ``devices.capture.type`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "capture", "type")


def _loaded_playback_type(config_path: Path) -> str | None:
    """The ``devices.playback.type`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "playback", "type")


def _loaded_playback_filename(config_path: Path) -> str | None:
    """The ``devices.playback.filename`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "playback", "filename")


@doctor_check(order=51.6, group="audio")
def check_audio_runtime_plan() -> CheckResult:
    """Explainable SSOT check for audio latency/coupling knobs."""

    from jasper.audio_runtime_plan import build_audio_runtime_plan_from_system

    plan = build_audio_runtime_plan_from_system()
    summary = (
        f"profile={plan.profile_id}, route={plan.route_mode}, "
        f"route_profile={plan.route_profile.route_id}, "
        f"route_hash={plan.route_config_hash}, "
        f"coupling={plan.setting('JASPER_FANIN_CAMILLA_COUPLING').value}, "
        f"camilla={plan.setting('JASPER_CAMILLA_CHUNKSIZE').value}/"
        f"{plan.setting('JASPER_CAMILLA_TARGET_LEVEL').value}, "
        f"outputd={plan.setting('JASPER_OUTPUTD_PERIOD_FRAMES').value}/"
        f"{plan.setting('JASPER_OUTPUTD_DAC_BUFFER_FRAMES').value}, "
        f"content_buffer={plan.setting('JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES').value}, "
        f"fanin={plan.setting('JASPER_FANIN_INPUT_BUFFER_FRAMES').value}/"
        f"{plan.setting('JASPER_FANIN_OUTPUT_BUFFER_FRAMES').value}"
    )
    if plan.errors:
        return CheckResult(
            "audio runtime plan",
            "fail",
            summary + "; " + "; ".join(plan.errors),
        )
    if plan.warnings:
        return CheckResult(
            "audio runtime plan",
            "warn",
            summary + "; " + "; ".join(plan.warnings[:3]),
        )
    return CheckResult("audio runtime plan", "ok", summary)


@doctor_check(order=51.65, group="audio")
def check_route_latency_evidence() -> CheckResult:
    """Low-latency route claims require a fresh measured artifact."""

    from jasper.audio_runtime_plan import build_audio_runtime_plan_from_system
    from jasper.audio_validation import (
        ROUTE_LATENCY_MIC_ID,
        ROUTE_LATENCY_P95_BUDGET_MS,
        ROUTE_LATENCY_PROFILE,
        ROUTE_LATENCY_STALE_AFTER,
        artifact_directory,
        assess_route_latency_artifact,
        load_latest_artifact,
    )

    plan = build_audio_runtime_plan_from_system()
    route = plan.route_profile
    if not route.low_latency_claim:
        return CheckResult(
            "route latency evidence",
            "ok",
            f"route_profile={route.route_id} has no low-latency claim",
        )
    if plan.errors:
        return CheckResult(
            "route latency evidence",
            "fail",
            f"route_profile={route.route_id}, route_hash={plan.route_config_hash}, "
            "runtime plan errors block latency certification: "
            + "; ".join(plan.errors),
        )

    dac_id = None if plan.profile_id == "unknown" else plan.profile_id
    result = load_latest_artifact(
        artifact_directory(),
        mic_id=ROUTE_LATENCY_MIC_ID,
        dac_id=dac_id,
        profile=ROUTE_LATENCY_PROFILE,
        max_age=ROUTE_LATENCY_STALE_AFTER,
    )
    summary = assess_route_latency_artifact(
        result,
        route_config_hash=plan.route_config_hash,
        expected_identity=plan.route_latency_identity(),
    )
    detail = (
        f"route_profile={route.route_id}, route_hash={plan.route_config_hash}, "
        f"artifact_status={summary.get('status')}, state={summary.get('state')}, "
        f"p95_ms={summary.get('p95_ms')}, p99_ms={summary.get('p99_ms')}, "
        f"samples={summary.get('sample_count')}, "
        f"duration_seconds={summary.get('duration_seconds')}, "
        f"certified={summary.get('certified_percentiles')}, "
        f"config_match={summary.get('config_match')}, "
        f"issues={summary.get('issues')}, "
        f"artifact={summary.get('artifact_path')}"
    )
    status = str(summary.get("status") or "fail")
    if status in {"pass", "warn"}:
        live_issues = _route_live_state_issues_for_doctor(plan)
        if live_issues:
            status = "fail"
            detail += f", live_issues={list(live_issues)}"
    if status == "pass":
        return CheckResult("route latency evidence", "ok", detail)
    if status == "warn":
        return CheckResult("route latency evidence", "warn", detail)
    return CheckResult(
        "route latency evidence",
        "fail",
        detail + "; Run route-latency validation for usb_low_latency_48k; "
        "p95 must be "
        f"<={ROUTE_LATENCY_P95_BUDGET_MS:g} ms with >=200 impulses over >=5 minutes, "
        "and promotion p99 requires >=1000 impulses over >=30 minutes.",
    )


@doctor_check(order=51.68, group="audio")
def check_fanin_coupling_value() -> CheckResult:
    """The persisted fan-in coupling must be a RECOGNIZED token.

    A migrating box may carry ``JASPER_FANIN_CAMILLA_COUPLING=transport_pipe`` (the
    coupling REMOVED 2026-07-11) or a typo. ``resolve_coupling`` fails such a value
    safe to loopback at daemon start, and the ``--auto`` reconciler converges it
    loudly (``event=…result=removed_coupling_failsafe``); this surfaces the stale
    value until that pass runs so the operator knows the persisted file names a
    transport that no longer exists.
    """
    from jasper.fanin.coupling_reconcile import FANIN_ENV_PATH
    from jasper.fanin_coupling import COUPLING_ENV_VAR, coupling_value_removed
    from jasper.env_file import read_value

    label = "fan-in coupling value"
    try:
        text = Path(FANIN_ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        return CheckResult(label, "ok", "no fanin.env — coupling defaults to loopback")
    raw = read_value(text, COUPLING_ENV_VAR)
    if coupling_value_removed(raw):
        return CheckResult(
            label,
            "warn",
            f"{COUPLING_ENV_VAR}={raw!r} in {FANIN_ENV_PATH} names a removed/unknown "
            "transport (e.g. the deleted transport_pipe coupling); it fails safe to "
            "loopback. Run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile loopback (or --auto) to converge the box "
            "and clean the file.",
        )
    return CheckResult(label, "ok", f"{COUPLING_ENV_VAR}={raw or '(unset → loopback)'}")


@doctor_check(order=51.7, group="audio")
def check_fanin_coupling() -> CheckResult:
    """The transport intent must match the loaded CamillaDSP graph.

    A mismatch is a half-applied arm/disarm: the dangerous one is
    intent=loopback but a ``RawFile`` config is loaded — CamillaDSP then reads a
    pipe no writer feeds and crash-loops on its statefile config (the jts5
    2026-06-27 failure mode). Under ``shm_ring`` (P2) both ends are ALSA ioplug
    devices — capture ``jts_ring_capture`` (Ring A) + playback
    ``jts_ring_playback`` (Ring B) — AND ``JASPER_OUTPUTD_CONTENT_BRIDGE`` must be
    ``shm_ring``: this check catches the PARTIAL flip (one end ring, the other
    ALSA/direct) that strands a ring. The fix is to re-run the ordered
    reconciler: ``jasper-fanin-coupling-reconcile <intent>``.
    """
    from jasper.fanin.coupling_reconcile import read_persisted_coupling
    from jasper.fanin_coupling import (
        COUPLING_SHM_RING,
        OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
        OUTPUTD_CONTENT_BRIDGE_SHM_RING,
        RING_CAPTURE_DEVICE,
        RING_PLAYBACK_DEVICE,
        resolve_outputd_content_bridge,
    )
    from jasper.audio_runtime_plan import DEFAULT_OUTPUTD_ENV_PATH
    from jasper.env_file import read_value

    label = "fan-in coupling"
    coupling = read_persisted_coupling()
    # _active_camilla_config_path returns (statefile, active_config_path|None);
    # the active path is what CamillaDSP actually loaded. Fall back to the JTS
    # sound config when the statefile names nothing.
    _, active_path = _active_camilla_config_path()
    config_path = Path(active_path) if active_path else Path(
        "/var/lib/camilladsp/configs/sound_current.yml"
    )
    try:
        outputd_env = Path(DEFAULT_OUTPUTD_ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        outputd_env = ""
    outputd_bridge = resolve_outputd_content_bridge(
        read_value(outputd_env, OUTPUTD_CONTENT_BRIDGE_ENV_VAR)
    )
    # Ring B env coherence: shm_ring REQUIRES the outputd bridge to match, and any
    # NON-ring coupling must NOT carry a stale shm_ring bridge (a partial flip that
    # points outputd at a ring nobody writes).
    if coupling == COUPLING_SHM_RING and outputd_bridge != OUTPUTD_CONTENT_BRIDGE_SHM_RING:
        return CheckResult(
            label,
            "warn",
            f"intent={coupling} but {OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={outputd_bridge} "
            f"in {DEFAULT_OUTPUTD_ENV_PATH} (expected shm_ring); PARTIAL flip — "
            "outputd reads snd-aloop while fan-in writes Ring A. Run: sudo "
            "/opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring",
        )
    if coupling != COUPLING_SHM_RING and outputd_bridge == OUTPUTD_CONTENT_BRIDGE_SHM_RING:
        return CheckResult(
            label,
            "warn",
            f"intent={coupling} but stale {OUTPUTD_CONTENT_BRIDGE_ENV_VAR}=shm_ring "
            f"remains in {DEFAULT_OUTPUTD_ENV_PATH}; outputd waits on Ring B that "
            "CamillaDSP no longer writes — run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile loopback",
        )
    capture = _loaded_capture_type(config_path)
    if capture is None:
        # No JTS config loaded yet (fresh box / non-JTS graph) — nothing to
        # contradict the intent. Report the intent so the comb has a verdict.
        return CheckResult(label, "ok", f"intent={coupling}; no loaded capture to compare")

    if coupling == COUPLING_SHM_RING:
        # Ring A capture + Ring B playback are BOTH ALSA ioplug devices — the
        # loaded graph must name jts_ring_capture AND jts_ring_playback, or the
        # coherent env pair above landed but the loaded config is stale/half-ring
        # (the built-in-revert class: a camilla restart re-seeded loopback).
        capture_device = _loaded_device_field(config_path, "capture", "device")
        playback_device = _loaded_device_field(config_path, "playback", "device")
        ring_mismatches: list[str] = []
        if capture != "Alsa" or capture_device != RING_CAPTURE_DEVICE:
            ring_mismatches.append(
                f"capture={capture}/{capture_device or '(missing)'} "
                f"(expected Alsa/{RING_CAPTURE_DEVICE})"
            )
        if playback_device != RING_PLAYBACK_DEVICE:
            ring_mismatches.append(
                f"playback_device={playback_device or '(missing)'} "
                f"(expected {RING_PLAYBACK_DEVICE})"
            )
        if not ring_mismatches:
            return CheckResult(
                label,
                "ok",
                f"{coupling} (capture={RING_CAPTURE_DEVICE}, "
                f"playback={RING_PLAYBACK_DEVICE}, bridge={outputd_bridge})",
            )
        return CheckResult(
            label,
            "warn",
            f"intent={coupling} but loaded graph is not the ring config: "
            f"{'; '.join(ring_mismatches)}; a camilla restart may have re-seeded "
            "loopback (finding-5 revert) — run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile shm_ring",
        )

    # Non-ring intent (loopback). The env
    # pair may be coherent (loopback/direct) yet the LOADED graph still name the
    # ring ioplug devices — a disarm whose camilla step failed leaves a stale ring
    # config that captures a writer-dead Ring A (zero-fill silence) while the env
    # reads clean. A type-only "capture==Alsa" check reads GREEN through that
    # permanent-silence state (the mirror of the shm_ring finding-5 branch above,
    # for the disarm direction). So inspect the device names too.
    capture_device = _loaded_device_field(config_path, "capture", "device")
    playback_device = _loaded_device_field(config_path, "playback", "device")
    stale_ring_devices = [
        f"{lane}={dev}"
        for lane, dev in (("capture", capture_device), ("playback", playback_device))
        if dev in (RING_CAPTURE_DEVICE, RING_PLAYBACK_DEVICE)
    ]
    if stale_ring_devices:
        return CheckResult(
            label,
            "warn",
            f"intent={coupling} but the loaded graph still names ring ioplug "
            f"device(s): {'; '.join(stale_ring_devices)}; a disarm's camilla step "
            "likely failed — CamillaDSP captures a writer-dead ring (silence) while "
            "the env reads clean. Run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile loopback",
        )
    expected = "Alsa"
    if capture == expected:
        playback = _loaded_playback_type(config_path)
        playback_path = _loaded_playback_filename(config_path)
        if playback == "File" and playback_path != "/run/jasper-snapserver/snapfifo":
            return CheckResult(
                label,
                "warn",
                f"intent={coupling} but loaded playback is a non-Snapcast File "
                f"sink ({playback_path or '(missing)'}); a stale File sink left by "
                "the removed transport_pipe coupling — run: "
                "sudo /opt/jasper/.venv/bin/"
                "jasper-fanin-coupling-reconcile loopback",
            )
        return CheckResult(label, "ok", f"{coupling} (capture={capture})")
    return CheckResult(
        label,
        "warn",
        f"intent={coupling} but loaded capture={capture} (expected {expected}); "
        f"half-applied transition — run: "
        f"sudo /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile {coupling}",
    )


def _jts_ring_path_for(pcm: str) -> str | None:
    """The SHM ring-file path a given inert PCM's open probe would create,
    or None if the PCM name is not one of ours. Derived from _JTS_RING_SHM_DIR
    (so tests can repoint the dir) + the basename registered in _JTS_RING_PCMS
    (which mirrors deploy/alsa/conf.d/60-jts-ring.conf's `path` values)."""
    for name, _tool, ring_basename in _JTS_RING_PCMS:
        if name == pcm:
            return os.path.join(_JTS_RING_SHM_DIR, ring_basename)
    return None


def _jts_ring_pcm_resolves(pcm: str, tool: str) -> tuple[bool, str]:
    """Open-probe one inert jts_ring PCM. Success means ALSA resolved the
    conf.d name AND dlopen()ed the ioplug .so AND the writer-dead/no-reader
    silence path terminated. A 1-second probe against an absent ring is
    safe: the ioplug free-runs (playback) or emits timer-paced silence
    (capture) rather than blocking (the lab resolvability-step contract).

    Leaves no residue: the ioplug open path is create-or-attach
    (O_RDWR|O_CREAT|O_EXCL), so probing an ABSENT ring CREATES the ring file.
    A doctor-created ring would (a) violate P1's inertness invariant ("no ring
    file exists until P2 arms") on every box after every deploy, and (b) poison
    P2's first arm, because a valid-magic ring carrying the conf.d PLACEHOLDER
    geometry is a fail-closed open error (only magic-less files are reclaimed).
    So we snapshot the ring path's existence before the probe and unlink ONLY a
    file the probe itself created. A live armed ring pre-exists (it is in the
    "existed before" set and is never unlinked; it also EBUSYs the probe via the
    SPSC guard), so this can never remove a ring in use.

    Returns (ok, detail). detail carries the tail of stderr on failure so
    a broken registration (e.g. the -DPIC "undefined symbol: snd_dlsym_start"
    class) is legible, not just "probe failed".
    """
    if not shutil.which(tool):
        return False, f"{tool} not found"
    ring_path = _jts_ring_path_for(pcm)
    pre_existed = bool(ring_path) and os.path.exists(ring_path)
    # arecord -> /dev/null (discard captured silence); aplay -> /dev/zero
    # (feed silence in). Both 2ch/48k/S16_LE/1s, matching the lab probe.
    sink = "/dev/null" if tool == "arecord" else "/dev/zero"
    try:
        proc = _run(
            [tool, "-D", pcm, "-c", "2", "-r", "48000", "-f", "S16_LE",
             "-d", "1", sink],
            timeout=6.0,
        )
    except subprocess.TimeoutExpired:
        return False, "open probe hung (>6 s) — ioplug no-reader/no-writer path may be broken"
    finally:
        # Remove a ring file the probe created (it did not exist beforehand).
        # Best-effort: a failure to unlink must not turn a clean probe into a
        # doctor error, but the residue would still be visible next run.
        if ring_path and not pre_existed and os.path.exists(ring_path):
            try:
                os.unlink(ring_path)
            except OSError:
                pass
    if proc.returncode == 0:
        return True, "resolved"
    err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
    if len(err) > 160:
        err = err[:157] + "..."
    return False, err or f"{tool} exit {proc.returncode}"


@doctor_check(order=51.8, group="audio", exclusive_group="audio-probe")
def check_ring_platform_assets() -> CheckResult:
    """Verify the jts_ring transport platform assets are present and the
    ioplug actually dlopens (audio-graph consolidation P1).

    Three assets ship INERT in P1: the compiled ioplug .so, the conf.d
    PCM definitions (pcm.jts_ring_capture / pcm.jts_ring_playback), and
    the /dev/shm/jts-ring directory. Nothing opens them yet — the default
    coupling is still loopback — but the platform must be correctly staged
    for P2 to arm it, and a broken .so (the -DPIC registration class) or a
    missing asset should surface here rather than at first arm.

    Statuses:
      ok    — .so + conf.d + shm dir present, both PCMs open-probe cleanly.
              CAVEAT: this cannot distinguish a freshly-built .so from a STALE
              one left by a failed rebuild (the 2026-07-02 class) — a stale but
              structurally-valid .so open-probes fine and reads ok here. The
              install transcript's build-failure WARN is the signal for that;
              ioplug-vs-Rust protocol-drift detection is P2's job (when the .so
              becomes load-bearing). See ring-platform.sh.
      warn  — an asset is MISSING (a first-ever build failed, or drift). P1 is
              inert and loopback remains the transport, so a missing .so is
              degraded, not broken — the next deploy rebuilds. (This flips to
              fail after P9, when the ioplug becomes load-bearing.)
      fail  — the .so is installed but a PCM fails to open. In the inert
              phase that is a real defect (bad registration / arch mismatch,
              e.g. the -DPIC class), which would break P2's arm. The EBUSY
              exception is called out below.

    Probe leaves no residue: it opens each device for 1 s against an absent
    ring, exercising only the writer-dead / no-reader silence path (feeds or
    discards silence). Because the ioplug open path is create-or-attach, the
    probe would create the ring file; _jts_ring_pcm_resolves snapshots and
    unlinks only what it created, so P1's "no ring file until P2 arms"
    invariant holds after every deploy.

    Armed-state aware (P2): when a coupling is ARMED (the persisted
    JASPER_FANIN_CAMILLA_COUPLING is shm_ring) the ring has a live reader/writer,
    so the ioplug's SPSC guard EBUSYs the open-probe — which is NOT a defect. In
    that state this check does NOT open-probe (the live ring must not be
    disturbed); it verifies asset PRESENCE only and defers the "is the armed ring
    coherent + alive" verdict to check_ring_coupling_coherent. The open-probe path
    below runs only in the INERT phase (loopback default), where an EBUSY genuinely
    would indicate a stray lab arm or a stuck ring.
    """
    label = "ring platform"
    # Pass the module-level constants (which tests monkeypatch, and which alias the
    # ring_assets SSOT) so the presence snapshot honors a repointed path.
    presence = ring_assets.ring_asset_presence(
        plugin_dir=_JTS_RING_ALSA_PLUGIN_DIR,
        conf_d=_JTS_RING_CONF_D,
        shm_dir=_JTS_RING_SHM_DIR,
    )
    missing = list(presence.missing())

    # Is a ring coupling armed? Read the persisted intent (fail-safe to loopback).
    try:
        from jasper.fanin.coupling_reconcile import read_persisted_coupling
        from jasper.fanin_coupling import COUPLING_SHM_RING

        ring_armed = read_persisted_coupling() == COUPLING_SHM_RING
    except (ImportError, OSError):
        ring_armed = False

    if missing:
        if ring_armed:
            # ARMED but an asset is gone: the ring is load-bearing now, so this is
            # a genuine failure (the ioplug/conf.d must exist for the armed graph).
            return CheckResult(
                label,
                "fail",
                "shm_ring is ARMED but a ring-platform asset is missing: "
                + "; ".join(missing)
                + " — the armed graph cannot resolve its ring devices; "
                "disarm (jasper-fanin-coupling-reconcile loopback) or redeploy.",
            )
        # Inert phase: a missing asset means the ring platform is
        # unavailable, but loopback still carries audio — degraded, not
        # a hard failure. Redeploy to rebuild/replace.
        return CheckResult(
            label,
            "warn",
            "inert platform incomplete (loopback still active): "
            + "; ".join(missing)
            + " — redeploy (bash scripts/deploy-to-pi.sh) to rebuild",
        )

    if ring_armed:
        # Assets present AND armed: do NOT open-probe (the live ring EBUSYs the
        # SPSC guard — expected, not a defect). Report ok here; the coherence
        # + liveness verdict is check_ring_coupling_coherent's job.
        return CheckResult(
            label,
            "ok",
            "ioplug + conf.d + /dev/shm/jts-ring present; shm_ring ARMED "
            "(open-probe skipped — live ring; see 'ring coupling' check)",
        )

    # All assets present AND inert — the .so being installed means it MUST dlopen
    # and register; a failure to open is a genuine defect.
    probe_failures: list[str] = []
    for pcm, tool, _ring_basename in _JTS_RING_PCMS:
        ok, detail = _jts_ring_pcm_resolves(pcm, tool)
        if not ok:
            probe_failures.append(f"{pcm}: {detail}")
    if probe_failures:
        # EBUSY is NOT a registration defect: on a lab-armed box (the ring-proto
        # experiment live, or after P2 arms a coupling) the ring already has a
        # live foreign reader/writer, and the ioplug's SPSC guard refuses the
        # probe with -EBUSY ("Device or resource busy"). The .so is fine — the
        # ring is simply in use. (check_ring_platform_assets and its "probe is
        # safe anytime" docstring assume the inert phase; P2 must make this check
        # armed-state-aware so an armed ring reports ok/skip, not fail.)
        joined = "; ".join(probe_failures)
        if re.search(r"resource busy|EBUSY|Device or resource busy", joined, re.I):
            remediation = (
                "ring is in use (a live reader/writer already owns it — e.g. a "
                "lab arm) — not a registration defect; re-run with the ring "
                "disarmed / camilla stopped to probe the .so."
            )
        else:
            remediation = (
                "Rebuild the ioplug (check -DPIC / arch); the .so is present but "
                "ALSA can't use it."
            )
        return CheckResult(
            label,
            "fail",
            f"ioplug .so installed but PCM open failed: {joined}. {remediation}",
        )
    return CheckResult(
        label,
        "ok",
        f"ioplug + conf.d + /dev/shm/jts-ring staged (inert); "
        f"{', '.join(name for name, _tool, _ring in _JTS_RING_PCMS)} resolve",
    )


@doctor_check(order=51.9, group="audio")
def check_ring_geometry_coherence() -> CheckResult:
    """Verify the Ring-A geometry agrees across env, conf.d, and on-disk (defect A).

    The ring geometry must match or CamillaDSP's ioplug attach fails hard
    (hw_params EINVAL + ``attach_fatal reason=ring header does not match expected
    geometry`` → crash-loop → start-limit-hit). ``n_slots`` is checked on THREE
    axes; the on-disk header adds a fourth check on ``period_frames`` (the ring slot
    IS one outputd period, so a stale period fails the attach even with matching
    slots — Nit-7, 2026-07-05):

      1. fan-in's resolved ``JASPER_FANIN_RING_SLOTS`` (jasper.env -> fanin.env
         systemd env chain, default 2)
      2. the conf.d ``jts_ring_capture`` ``n_slots`` (the ioplug attach authority)
      3. the on-disk ``program.ring`` header ``n_slots`` (what the writer created)
      4. the on-disk header ``period_frames`` vs the conf.d ``period_frames``

    The 2026-07-06 default migration class is old 8-slot state making fan-in or
    the existing ring file present 8 slots while the conf.d pins 2. The coupling
    reconciler preflights + self-heals this at arm time (and on the CONFIRM path
    for an already-armed box); this check is the standing surface that catches
    drift on a live box.

    Skips cleanly when the coupling is NOT shm_ring (the ring is inert — the env /
    conf.d values are placeholders that nothing opens, so a "mismatch" is not a
    live defect). On an armed box a mismatch is ``fail`` (the graph cannot run);
    an indeterminate conf.d / env is ``warn`` (redeploy to reinstall).
    """
    label = "ring geometry"
    try:
        from jasper.fanin.coupling_reconcile import (
            FANIN_ENV_PATH,
            read_persisted_coupling,
            resolve_effective_fanin_ring_slots,
        )
        from jasper.fanin_coupling import (
            COUPLING_SHM_RING,
            RING_SLOTS_ENV_VAR,
        )
    except ImportError as e:  # pragma: no cover - always importable in prod
        return CheckResult(label, "warn", f"ring modules unavailable: {e}")

    if read_persisted_coupling(FANIN_ENV_PATH) != COUPLING_SHM_RING:
        return CheckResult(
            label, "ok",
            "skipped — shm_ring not armed (Ring A geometry is inert; nothing opens it)",
        )

    # Axis 1: fan-in's resolved env slot count (fail-loud on a bad value).
    try:
        fanin_text = Path(FANIN_ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        fanin_text = ""
    resolution = resolve_effective_fanin_ring_slots(fanin_text)
    if resolution.value is None:
        return CheckResult(
            label, "fail",
            f"effective {RING_SLOTS_ENV_VAR} from {resolution.source} is invalid: "
            f"{resolution.error}. shm_ring is armed — fan-in will refuse to create "
            "the ring. Clear the stale value.",
        )
    fanin_slots = resolution.value

    # Axis 2: the conf.d attach authority.
    conf_slots = ring_assets.ring_conf_n_slots(
        ring_assets.RING_A_CONF_PCM, _JTS_RING_CONF_D
    )
    if conf_slots is None:
        return CheckResult(
            label, "warn",
            f"conf.d ({_JTS_RING_CONF_D}) has no single n_slots for "
            f"pcm.{ring_assets.RING_A_CONF_PCM}; Ring A geometry is indeterminate — "
            "redeploy to reinstall the ring conf.d.",
        )
    if fanin_slots != conf_slots:
        return CheckResult(
            label, "fail",
            f"Ring A slot mismatch: JASPER_FANIN_RING_SLOTS resolves to {fanin_slots} "
            f"but conf.d pcm.{ring_assets.RING_A_CONF_PCM} pins n_slots={conf_slots}. "
            "shm_ring is armed — CamillaDSP's ioplug attach fails (hw_params EINVAL) "
            "and crash-loops. Run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile shm_ring (it self-heals a stale env), "
            "or match the two values.",
        )

    # Axis 3: the on-disk ring header (what the writer actually created).
    header = ring_assets.read_ring_header(ring_assets.RING_A_PROGRAM_FILE)
    if not header.valid:
        # Armed but no coherent on-disk ring yet: fan-in may be between restarts,
        # or the ring was cleared. The env/conf.d agree, so the next writer create
        # is coherent — noteworthy, not a hard failure.
        return CheckResult(
            label, "warn",
            f"env + conf.d agree (n_slots={fanin_slots}) but {ring_assets.RING_A_PROGRAM_FILE} "
            "has no valid ring header yet (fan-in restarting / ring cleared). It "
            "will be created coherently on the next fan-in start.",
        )
    if header.n_slots != conf_slots:
        return CheckResult(
            label, "fail",
            f"on-disk Ring A ({ring_assets.RING_A_PROGRAM_FILE}) has n_slots="
            f"{header.n_slots} but env + conf.d expect {conf_slots}. A stale ring "
            "file from a prior geometry blocks the ioplug attach. Run: sudo "
            "/opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring "
            "(it deletes a geometry-mismatched ring file before re-arming).",
        )
    # period_frames is the SECOND on-disk geometry axis (the ring slot IS one
    # outputd period): a file with matching n_slots but a stale period_frames also
    # fails the ioplug attach — the exact confusing daemon-level error the
    # preflights exist to pre-empt. Compare against the conf.d's pinned period when
    # it is readable (indeterminate conf.d period → skip this axis, don't guess).
    conf_period = ring_assets.ring_conf_period_frames(_JTS_RING_CONF_D)
    if conf_period is not None and header.period_frames != conf_period:
        return CheckResult(
            label, "fail",
            f"on-disk Ring A ({ring_assets.RING_A_PROGRAM_FILE}) has period_frames="
            f"{header.period_frames} but conf.d expects {conf_period} (n_slots match "
            f"at {header.n_slots}). A stale ring file from a prior period geometry "
            "blocks the ioplug attach. Run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile shm_ring (it deletes a geometry-"
            "mismatched ring file before re-arming).",
        )
    return CheckResult(
        label, "ok",
        f"Ring A geometry coherent across env + conf.d + on-disk header "
        f"(n_slots={header.n_slots}, period_frames={header.period_frames})",
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

    try:
        payload = _read_status_socket_bytes(_OUTPUTD_STATUS_SOCKET, timeout=2.0)
    except OSError as e:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but STATUS probe at {_OUTPUTD_STATUS_SOCKET} failed: {e}. "
            "Without STATUS doctor cannot verify DAC ownership, buffers, "
            "xruns, or work-loop progress.",
        )

    body = payload.decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but STATUS returned invalid JSON: {e}",
        )
    if not isinstance(data, dict):
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but STATUS root is {type(data).__name__}, expected object",
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
    if not isinstance(content, dict):
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS missing content{}",
        )
    if not isinstance(dac, dict):
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS missing dac{}",
        )
    from jasper.fanin.coupling_reconcile import read_persisted_coupling
    from jasper.fanin_coupling import COUPLING_SHM_RING
    from jasper.audio_runtime_plan import (
        DEFAULT_CAMILLA2_STATEFILE_PATH,
        DEFAULT_CAMILLA_STATEFILE_PATH,
        output_endpoint_evidence_from_statefiles,
        transport_coherence_errors,
        transport_topology_for_coupling,
    )

    coupling = read_persisted_coupling()
    topology = transport_topology_for_coupling(
        coupling,
        outputd_env=outputd_env,
    )
    expected_content_source = topology.outputd_content_source
    actual_content_source = content.get("source")
    if actual_content_source != expected_content_source:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"content.source={actual_content_source!r}; expected "
            f"{expected_content_source!r} for persisted coupling={coupling!r}. "
            "Run jasper-fanin-coupling-reconcile to restart outputd onto the "
            "persisted topology.",
        )
    live_outputd_env = dict(outputd_env)
    live_outputd_env["JASPER_OUTPUTD_CONTENT_PCM"] = str(content.get("pcm") or "")
    endpoint_evidence = output_endpoint_evidence_from_statefiles(
        DEFAULT_CAMILLA_STATEFILE_PATH,
        DEFAULT_CAMILLA2_STATEFILE_PATH,
    )
    transport_evidence_warning = ""
    if (
        endpoint_evidence.devices is None
        or not endpoint_evidence.endpoint_recognized
    ):
        evidence_detail = "; ".join(endpoint_evidence.errors) or (
            "loaded graph does not target a registered output endpoint"
        )
        transport_evidence_warning = (
            "post-DSP transport coherence unknown: " + evidence_detail
        )
    else:
        transport_errors = transport_coherence_errors(
            coupling=coupling,
            outputd_env=live_outputd_env,
            camilla_devices=endpoint_evidence.devices,
        )
        if transport_errors:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "; ".join(transport_errors)
                + ". Run jasper-audio-hardware-reconcile to restore the paired "
                "CamillaDSP playback/outputd capture lane, then re-run "
                "jasper-fanin-coupling-reconcile only if the coupling check also "
                "reports Ring A/Ring B drift.",
            )
    local_pipe_detail = f"content_source={actual_content_source}"
    ring_detail: str = ""
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
    if coupling == COUPLING_SHM_RING:
        # Ring B (SHM ping-pong content ring): outputd reads the post-DSP program
        # from an n-slot SHM ring, NOT an ALSA capture PCM (AlsaBackend::new never
        # opens the content PCM under shm_ring). content.buffer_frames is therefore
        # a synthetic period-sized stand-in, so the generic ">= 2x period" ALSA
        # jitter-margin floor does not apply — a bounded n-slot queue is not an ALSA
        # buffer, and every shm_ring box would otherwise structurally fail that floor
        # (content.buffer_frames == period < 2 x period). Validate the TRUE ring
        # geometry from content.ring instead (the honesty contract outputd publishes
        # next to the synthetic; capacity_frames == n_slots x slot_frames).
        if not isinstance(content_buffer, int) or content_buffer < period_frames:
            return CheckResult(
                "jasper-outputd",
                "fail",
                f"shm_ring content.buffer_frames={content_buffer!r}; expected >= "
                f"one period ({period_frames})",
            )
        ring = content.get("ring")
        if not isinstance(ring, dict):
            return CheckResult(
                "jasper-outputd",
                "fail",
                "content.source='shm_ring' but STATUS missing content.ring geometry "
                "contract (n_slots/slot_frames/capacity_frames). Redeploy outputd.",
            )
        ring_slots = ring.get("slots")
        ring_slot_frames = ring.get("slot_frames")
        ring_capacity = ring.get("capacity_frames")
        if not isinstance(ring_slots, int) or ring_slots < 2:
            return CheckResult(
                "jasper-outputd",
                "fail",
                f"shm_ring content.ring.slots={ring_slots!r}; expected >= 2 "
                "(ping-pong minimum)",
            )
        if not isinstance(ring_slot_frames, int) or ring_slot_frames != period_frames:
            return CheckResult(
                "jasper-outputd",
                "fail",
                f"shm_ring content.ring.slot_frames={ring_slot_frames!r}; expected "
                f"== dac.period_frames ({period_frames}) — the ring slot must match "
                "the DAC period.",
            )
        expected_capacity = ring_slots * ring_slot_frames
        if not isinstance(ring_capacity, int) or ring_capacity != expected_capacity:
            return CheckResult(
                "jasper-outputd",
                "fail",
                f"shm_ring content.ring.capacity_frames={ring_capacity!r}; expected "
                f"n_slots*slot_frames ({expected_capacity})",
            )
        # Runtime health (occupancy/attach) rides the top-level shm_ring block; a
        # transient empty ring is normal (idle), so surface it in the detail without
        # gating on it here.
        shm_ring_block = data.get("shm_ring")
        ring_occupancy = (
            shm_ring_block.get("occupancy")
            if isinstance(shm_ring_block, dict) else None
        )
        ring_attached = (
            bool(shm_ring_block.get("attached", False))
            if isinstance(shm_ring_block, dict) else None
        )
        ring_detail = (
            f", shm_ring_slots={ring_slots}, shm_ring_slot_frames={ring_slot_frames}"
            f", shm_ring_capacity_frames={ring_capacity}"
            f", shm_ring_occupancy={ring_occupancy}"
            f", shm_ring_attached={ring_attached}"
        )
    elif not isinstance(content_buffer, int) or content_buffer < period_frames * 2:
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
    xrun_warning = _outputd_xrun_rate_warning(content, dac)
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
    if xrun_warning is not None:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but {xrun_warning}. xruns={content_xruns}/{dac_xruns}. "
            "A sustained, recent xrun rate means audible dropouts — check "
            "CPU contention (jasper-camilla RT scheduling), DAC buffer sizing "
            "(JASPER_OUTPUTD_DAC_BUFFER_FRAMES), and "
            "`journalctl -u jasper-outputd | grep xrun`.",
        )
    status = "warn" if transport_evidence_warning else "ok"
    transport_detail = (
        f", {transport_evidence_warning}" if transport_evidence_warning else ""
    )
    return CheckResult(
        "jasper-outputd",
        status,
        f"active, backend=alsa, frames_written={frames}, "
        f"content_buffer_frames={content_buffer}, dac_buffer_frames={dac_buffer}, "
        f"xruns={content_xruns}/{dac_xruns}, "
        f"content_empty_periods={content_empty}, "
        f"content_partial_periods={content_partial}, "
        f"content_eagain_count={content_eagain}, "
        f"{local_pipe_detail}, "
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
        f"{dual_detail}"
        f"{ring_detail}"
        f"{transport_detail}",
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

    try:
        payload = _read_status_socket_bytes(_OUTPUTD_STATUS_SOCKET, timeout=2.0)
    except OSError as e:
        return CheckResult(label, "ok", f"skipped — STATUS unreachable: {e}")

    body = payload.decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return CheckResult(label, "ok", "skipped — STATUS returned invalid JSON")
    if not isinstance(data, dict):
        return CheckResult(
            label,
            "ok",
            f"skipped — STATUS root is {type(data).__name__}, expected object",
        )

    reference_outputs = data.get("reference_outputs")
    if not isinstance(reference_outputs, dict):
        return CheckResult(label, "ok", "skipped — STATUS missing reference_outputs")
    if reference_outputs.get("chip_ref_pcm") is None:
        return CheckResult(label, "ok", "skipped — chip reference not configured")
    chip_ref_writer = reference_outputs.get("chip_ref_writer")
    if isinstance(chip_ref_writer, dict) and not bool(
        chip_ref_writer.get("active", chip_ref_writer.get("enabled", False))
    ):
        writer_status = str(chip_ref_writer.get("status") or "unknown")
        recovery = (
            "outputd is retrying the optional AEC reference device"
            if writer_status in {"connecting", "degraded"}
            else "the reference worker stopped; correct the device/config and "
            "restart jasper-outputd"
        )
        return CheckResult(
            label,
            "warn",
            "chip reference is desired but unavailable; speaker playback remains "
            f"active and {recovery} "
            f"(status={writer_status}, "
            f"open_errors={chip_ref_writer.get('open_error_count')}, "
            f"retries={chip_ref_writer.get('retry_count')})",
        )
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
