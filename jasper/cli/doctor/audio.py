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
)
from ...config import Config
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
# what it created — see _jts_ring_pcm_resolves, which owns the ONE
# `_JTS_RING_PCMS` table (jasper.cli.doctor.audio_runtime). This module used to
# carry a second copy of that table; it had zero readers, because the probe and
# the check are both re-exported from audio_runtime at the bottom of this file,
# so the copy could only ever be a second answer waiting to disagree.


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

def _soften_for_push_to_talk(
    result: CheckResult, presence: MicPresence,
) -> CheckResult:
    """Downgrade a *local* mic failure to an advisory when an accessory is paired.

    A speaker with no local microphone but a paired mic-bearing remote has its
    voice-input gate legitimately open (issue #2205), so a red "your microphone
    is missing" is the wrong register. These checks probe the local device, so
    they are the surface that can actually tell "no local mic" from "local mic
    present"; the `microphone` headline cannot, because it reads the OR verdict.

    The detail reports GATE state, never runtime state. The daemon half of
    issue #2205 has since landed, so such a box CAN answer — which makes the
    runtime claim tempting and no less unfounded. Saying "voice runs
    push-to-talk only" here would still be a present-tense claim about a daemon
    this check never looks at; whether it is actually up is a separate fact.

    The local finding stays visible (``warn``, original detail appended) so an
    operator can still see the local mic is gone. Only the register changes:
    expected-idle, not failure. A ``warn``/``ok`` result is returned untouched —
    this never upgrades a status.

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
    """Single headline for microphone presence — the one flag for "is there
    a mic?".

    Reads the reconciler's one canonical record via
    ``jasper.mic_presence.read_mic_presence`` and states present/absent + *why*
    in a single line. The downstream ``mic ALSA card`` / ``mic capture`` checks
    and the audio-open-failure log all defer to this same verdict instead of
    independently re-probing ALSA, so a missing mic is one yellow advisory —
    not a scatter of contradicting red failures. Absent is ``warn`` (never
    ``fail``): the reconciler parked voice and it auto-starts when a mic is
    reconnected or an actionable profile condition is resolved, so it's
    noteworthy, not broken.

    **What ``ok`` claims, precisely: the voice-input start gate is open.** Not
    that jasper-voice is running, and not that a *local* microphone exists —
    the record this reads is the OR of the local and accessory halves and
    carries no local probe (see ``jasper.mic_presence``). So the status
    deliberately does NOT drop to ``warn`` merely because an accessory
    satisfied the gate: the identical record shape also covers a box with a
    healthy non-XVF local mic (a custom ``JASPER_MIC_DEVICE``, a plain USB mic)
    that happens to have a remote paired, and warning there would be a
    permanent false yellow on a working speaker — the contradicting-checks
    scatter this single-reader design exists to prevent. The surfaces that CAN
    tell those apart are ``mic ALSA card`` and ``mic capture``: they probe the
    device and downgrade to ``warn`` naming issue #2205 when the local mic is
    genuinely missing. The detail line here reads "voice-input gate open"
    rather than "present", so an operator can tell the two apart at a glance
    without this check pretending to knowledge it does not have."""
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
    presence = read_mic_presence()
    if presence.absent_confirmed:
        return CheckResult(
            "mic ALSA card", "ok",
            "no usable microphone input — see the `microphone` check "
            "(voice is intentionally parked until its condition is resolved)",
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
        return _soften_for_push_to_talk(CheckResult(
            label, "fail",
            f"no card {card} / device {device} in `arecord -l` output. "
            f"The AEC bridge migrated to UDP in PR 2 and the old "
            f"LoopbackAEC card no longer exists — update "
            f"JASPER_MIC_DEVICE to `udp:9876` (or `Array` for chip-direct). "
            f"Verify with `aplay -l | grep Loopback` and "
            f"`systemctl status jasper-aec-bridge`.",
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
    presence = read_mic_presence()
    if presence.absent_confirmed:
        return CheckResult(
            "mic capture", "ok",
            "no usable microphone input (expected) — see the `microphone` "
            "check; voice is intentionally parked until its condition is resolved",
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
            # NOT softened: the device opened, so a local microphone IS present
            # — it is muted or misrouted. A paired accessory does not make that
            # expected, and "no local microphone" would be a lie here.
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
    # The reconciler-emitted final-edge format (JASPER_OUTPUTD_DAC_FORMAT in
    # /var/lib/jasper/outputd.env, which env_load sources). Read it, never
    # re-derive it from the registry: the emitted value is what outputd and the
    # chip-AEC alignment identity actually see, so this surfaces the one value
    # an operator can compare against the registry by hand. It does NOT detect
    # drift on its own — it prints a single value; registry-vs-emission drift
    # is caught by the reconcile contract tests in
    # tests/test_audio_hardware_reconcile.py. Unset/blank is the historical
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
    signal; the binding production chip-AEC gate is the fixed DAC-profile
    qualification (`resolve_chip_aec_dac_gate` in jasper/chip_aec_policy.py).
    The SRO clock verdict is diagnostic only. A
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
        # but the binding chip-AEC gate is fixed DAC qualification, not this
        # tag or the diagnostic SRO verdict. WARN so a
        # maintainer notices the drift risk; software AEC3 keeps echo cancelled
        # either way.
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
    profile = _dac_profile_for(dac_id)
    usb_ids = profile.usb_ids if profile is not None else ()
    usb_count = sum(
        len(re.findall(re.escape(usb_id), p.stdout, re.IGNORECASE))
        for usb_id in usb_ids
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

from . import audio_runtime as audio_runtime
from .audio_runtime import (
    _FANIN_EXPECTED_INPUTS,
    _FANIN_EXPECTED_OUTPUT_PCM,
    _OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM,
    _OUTPUTD_EXPECTED_CONTENT_PCM,
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
    check_route_latency_evidence,
)

__all__ = [
    "_FANIN_EXPECTED_INPUTS",
    "_FANIN_EXPECTED_OUTPUT_PCM",
    "_OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM",
    "_OUTPUTD_EXPECTED_CONTENT_PCM",
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
    "check_route_latency_evidence",
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

@doctor_check(order=28.5, group="audio")
def check_active_speaker_runtime_graph() -> CheckResult:
    """Fail closed if a roleful/protected topology is running flat stereo."""
    from jasper.active_speaker.runtime_contract import (
        GRAPH_PARKED_ALL_MUTED,
        classify_bass_extension_graph,
        classify_output_contract,
        parked_muted_exits,
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
        # #2135: the box declared roleful outputs but never staged a startup
        # graph, so the deploy parked it silent instead of failing. Nothing is
        # broken and nothing is audible — but the household has to finish (or
        # undo) commissioning, so this warns rather than passing green. The
        # exits are capability-aware: on a DAC with no active outputd lane
        # "finish crossover preview" can never succeed, so it is not offered.
        return CheckResult(
            "active speaker runtime graph",
            "warn",
            (
                f"parked silent for {contract.classification}: "
                f"{parked_muted_exits(topology)}"
            ),
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


@doctor_check(order=28.6, group="audio")
def check_active_speaker_topology_blockers() -> CheckResult:
    """Name the saved layout's unresolved blockers on a parked speaker.

    Before #2145 a roleful topology carrying any topology-level blocker made
    every deploy abort, so the blockers were impossible to miss — the install
    transcript was the notification. They no longer abort it: the parked graph
    is structurally silent (File sink, every output hard-muted), so a blocker
    that cannot make it unsafe no longer refuses it, and the box parks and takes
    the deploy. That is the right outcome, but it removes the loud signal, so
    this check restores one at the household's own diagnostic surface and names
    each blocker plus the wizard step that clears it.

    WARN, never FAIL: a parked speaker is silent, not broken, and nothing here
    is audible or at risk — the state is "commissioning is unfinished", which is
    the household's to finish, not an error to fix. `jasper-doctor` exits
    non-zero only on fails, so warning keeps a mid-commission box deployable,
    which is the whole point of #2145.

    Scoped to the parked outcome on purpose. A blocker-bearing topology that
    DOES have a staged graph still fails the deploy and is already reported by
    `check_active_speaker_runtime_graph`; repeating it here would be a second
    voice for one fact.
    """

    from jasper.active_speaker.runtime_contract import (
        PARKED_MUTED_STATUS,
        classify_output_contract,
        safe_graph_for_current_topology,
    )
    from jasper.output_topology import OutputTopologyError, load_output_topology_strict

    name = "active speaker topology blockers"
    try:
        topology = load_output_topology_strict()
    except OutputTopologyError as exc:
        return CheckResult(
            name, "fail", f"saved output topology is unavailable or invalid: {exc}"
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
                "Finish the speaker layout at http://<speaker>/sound/setup/"
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
            "Deploys now succeed in this state, so finish the speaker layout at "
            "http://<speaker>/sound/setup/ to bring it back to sound."
        ),
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
    """True if ``live_path`` is a content-addressed sibling of ``canonical``.

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
    affecting the audible graph at all — CamillaDSP's own statefile already
    self-persists the running candidate path independently. This check
    surfaces that gap for the OTHER readers who trust the canonical name
    (the multiroom follower fallback, operators, this doctor itself): the
    live graph is always the audible truth and is correct either way, so a
    mismatch is a WARN, never a FAIL.
    """
    from jasper.active_speaker.baseline_profile import (
        active_layer_a_fingerprint,
        baseline_config_path,
    )
    from jasper.active_speaker.profile import ActiveSpeakerConfigError

    label = "active speaker baseline canonical"
    statefile, live_path_raw = _active_camilla_config_path()
    if live_path_raw is None:
        # A missing/unreadable outputd statefile is already surfaced by the
        # checks that own it as a real failure (e.g. check_active_speaker_
        # runtime_graph fails when a roleful topology needs it); this check's
        # own scope is only "does canonical mirror the live baseline", which
        # cannot be evaluated at all here -- not applicable, not a warning.
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
