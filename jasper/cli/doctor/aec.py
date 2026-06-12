"""jasper-doctor checks — aec domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from ...audio_profile_state import (
    AecIntent,
    MicProbe,
    build_audio_profile_status,
    runtime_env_from_mapping,
)
from ...audio_validation import CHIP_AEC_PROFILE
from ...audio_validation import current_artifact_filter_kwargs as _audio_validation_filter_kwargs
from ...audio_validation import latest_artifact_summary as _audio_validation_summary
from ...env_load import parse_env_file as _shared_parse_env_file
from ._registry import doctor_check
from ._shared import (
    CheckResult,
    _CHIP_AEC_PASSIVE_REQUIRED_CHECKS,
    _parked_as_bonded_follower,
    _KNOWN_CHIP_AEC_PASSIVE_HARDWARE,
    _loopback_playback_active,
    _run,
    _sha256_file,
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

def _aec_profile_setting() -> str:
    """Read JASPER_AUDIO_INPUT_PROFILE from aec_mode.env.

    Empty string means pre-profile config; audio_profile_state infers the
    nearest legacy profile from JASPER_AEC_MODE + leg booleans.
    """

    p = Path("/var/lib/jasper/aec_mode.env")
    if not p.exists():
        return ""
    try:
        for line in p.read_text().split("\n"):
            line = line.strip()
            if line.startswith("JASPER_AUDIO_INPUT_PROFILE="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""

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

def _chip_aec_available_for_doctor() -> bool:
    try:
        from ...mics import xvf3800
        return xvf3800.is_recommended_firmware()
    except Exception:  # noqa: BLE001
        return False

def _audio_profile_status_for_doctor(
    *,
    bridge_active: bool | None = None,
    env: dict[str, str] | None = None,
    mic_probe: MicProbe | None = None,
) -> dict:
    """Build the same read-only audio-profile status used by /aec.

    The doctor is a one-shot CLI, but it still reads the reconciler-owned
    env file fresh so it reports the applied runtime env rather than only
    whatever the calling shell inherited.
    """

    if bridge_active is None:
        bridge_active = (
            _run(["systemctl", "is-active", "jasper-aec-bridge.service"])
            .stdout.strip() == "active"
        )
    if env is None:
        env = _shared_parse_env_file(
            os.environ.get("JASPER_ENV_FILE", "/etc/jasper/jasper.env"),
        )
    runtime = runtime_env_from_mapping(env, process_env=os.environ)

    if mic_probe is None:
        try:
            from ...mics import xvf3800
            capture_channels = xvf3800.capture_channels()
            mic_probe = MicProbe(
                xvf_present=xvf3800.is_present(),
                capture_channels=capture_channels,
                recommended_channels=(
                    xvf3800.RECOMMENDED_FIRMWARE.capture_channels
                ),
                display_name=xvf3800.DISPLAY_NAME,
            )
        except Exception:  # noqa: BLE001
            mic_probe = MicProbe(
                xvf_present=False,
                capture_channels=None,
                probe_error="firmware probe failed",
            )

    chip_available = (
        mic_probe.xvf_present
        and mic_probe.capture_channels == mic_probe.recommended_channels
    )
    return build_audio_profile_status(
        AecIntent(
            mode=_aec_mode_setting(),
            raw_enabled=_wake_leg_setting("JASPER_WAKE_LEG_RAW", True),
            dtln_enabled=_wake_leg_setting("JASPER_WAKE_LEG_DTLN", False),
            chip_aec_enabled=_wake_leg_setting(
                "JASPER_WAKE_LEG_CHIP_AEC", False,
            ),
            profile_selection=_aec_profile_setting(),
        ),
        runtime,
        mic_probe,
        bridge_active=bridge_active,
        chip_available=chip_available,
    )

def _assess_audio_profile(status: dict) -> CheckResult:
    profile = status.get("audio_profile") or {}
    mic = status.get("microphone") or {}
    raw_warnings = mic.get("warnings")
    warnings = raw_warnings if isinstance(raw_warnings, list) else []
    state = str(profile.get("state") or "unknown")
    active = profile.get("active") or "none"
    legs = mic.get("wake_legs")
    if isinstance(legs, list) and legs:
        legs_text = ", ".join(str(leg) for leg in legs)
    else:
        legs_text = "none"
    detail = (
        f"requested={profile.get('requested') or 'unknown'}, "
        f"active={active}, state={state}; "
        f"mode={mic.get('processing_mode') or 'unknown'}, "
        f"session={mic.get('session_source') or 'unknown'}, "
        f"legs={legs_text}"
    )
    if warnings:
        detail += "; " + " ".join(str(w) for w in warnings)

    if state in {"active", "disabled"} and not warnings:
        result = "ok"
    else:
        result = "warn"
    return CheckResult("Audio profile", result, detail)

@doctor_check(order=46, group="aec")
def check_audio_profile_runtime() -> CheckResult:
    """Summarise requested vs applied mic/AEC profile runtime truth."""

    return _assess_audio_profile(_audio_profile_status_for_doctor())

def _assess_audio_validation_summary(
    summary: dict[str, object],
    *,
    requested_profile: str | None,
) -> CheckResult:
    state = str(summary.get("state") or "unknown")
    status = str(summary.get("status") or "unknown")
    recommendation = str(summary.get("recommendation") or "none")
    validated_at = str(summary.get("validated_at") or "never")
    path = str(summary.get("artifact_path") or "unknown")
    detail = (
        f"profile={requested_profile or 'unknown'}, validation={state}, "
        f"status={status}, validated_at={validated_at}, "
        f"recommendation={recommendation}, path={path}"
    )
    reason = summary.get("reason")
    if reason:
        detail += f"; {reason}"

    if requested_profile != CHIP_AEC_PROFILE:
        return CheckResult(
            "Audio validation",
            "ok",
            detail + "; advisory because chip-AEC is not the requested profile",
        )
    if state == "current" and status == "pass":
        return CheckResult("Audio validation", "ok", detail)
    if _known_supported_chip_aec_passive_ok(summary):
        return CheckResult(
            "Audio validation",
            "ok",
            detail
            + "; known-supported xvf_chip_aec path passed passive hardware "
            "validation; optional acoustic drift/delay probe not implemented/run",
        )
    if recommendation in {"run_hardware_validation", "run_drift_delay_validation"}:
        command = "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout"
    else:
        command = "sudo jasper-audio-validate --stdout"
    return CheckResult(
        "Audio validation",
        "warn",
        detail + f"; advisory: consider `{command}` after chip-AEC is active",
    )

def _known_supported_chip_aec_passive_ok(summary: dict[str, object]) -> bool:
    """Return true when the current partial artifact is enough for operators.

    The artifact remains warn because no explicit acoustic drift/delay probe
    exists yet. For the known-good reference path, clean passive hardware
    evidence should not remain a product warning; for any other DAC path, the
    drift/delay gate is still required before recommending chip-AEC.
    """

    if str(summary.get("state") or "unknown") != "current":
        return False
    if str(summary.get("recommendation") or "none") != "run_drift_delay_validation":
        return False
    hardware = summary.get("hardware")
    if not isinstance(hardware, dict):
        return False
    mic_id = str(hardware.get("mic_id") or "unknown")
    dac_id = str(hardware.get("dac_id") or "unknown")
    if (mic_id, dac_id) not in _KNOWN_CHIP_AEC_PASSIVE_HARDWARE:
        return False
    statuses = summary.get("check_statuses")
    if not isinstance(statuses, dict):
        return False
    return all(
        statuses.get(check_name) == "pass"
        for check_name in _CHIP_AEC_PASSIVE_REQUIRED_CHECKS
    )

@doctor_check(order=47, group="aec")
def check_audio_validation_readiness() -> CheckResult:
    """Report latest schema-v1 validation artifact as advisory readiness."""

    profile_status = _audio_profile_status_for_doctor().get("audio_profile") or {}
    requested_profile = profile_status.get("requested")
    if requested_profile is not None:
        requested_profile = str(requested_profile)
    validation_filters = _audio_validation_filter_kwargs(
        requested_profile=requested_profile,
        system_env=_shared_parse_env_file(
            os.environ.get("JASPER_ENV_FILE", "/etc/jasper/jasper.env"),
        ),
    )
    return _assess_audio_validation_summary(
        _audio_validation_summary(**validation_filters),
        requested_profile=requested_profile,
    )

@doctor_check(order=45, group="aec")
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
    if _parked_as_bonded_follower():
        return CheckResult(
            "AEC bridge", "ok",
            "parked (bonded follower) — the dumb-follower profile stops "
            "voice + the AEC stack while paired; the leader owns the mic",
        )
    from ...mics import xvf3800
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

@doctor_check(order=48, group="aec")
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
    if _parked_as_bonded_follower():
        return CheckResult(
            "AEC bridge output", "ok",
            "parked (bonded follower) — the dumb-follower profile stops "
            "voice + the AEC stack while paired; the leader owns the mic",
        )
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

# How stale the bridge stats snapshot may be before the doctor falls
# back to journal parsing. The bridge rewrites it every 0.5 s, so 30 s
# of staleness means the snapshot belongs to a dead/old process.
_BRIDGE_STATS_FRESH_SEC = 30.0


def _assess_dtln_engine_from_stats(
    stats: dict, now: float,
) -> CheckResult | None:
    """Authoritative DTLN-leg verdict from the bridge's live stats
    snapshot (/run/jasper/aec_bridge_stats.json, `leg_engines.dtln`,
    written at startup by jasper/cli/aec_bridge.py). Returns None when
    the snapshot is stale or predates the leg_engines field — caller
    falls back to journal parsing, which is window-limited (a load
    failure ages out of the 10-min journal window; this surface
    doesn't)."""
    try:
        updated = float(stats.get("updated_epoch_sec", 0.0))
        leg = stats["leg_engines"]["dtln"]
        enabled = bool(leg["enabled"])
        loaded = bool(leg["loaded"])
        error = leg.get("error")
    except (KeyError, TypeError, ValueError):
        return None
    if now - updated > _BRIDGE_STATS_FRESH_SEC:
        return None
    if enabled and loaded:
        return CheckResult(
            "DTLN-aec engine", "ok",
            "loaded (per bridge stats snapshot; triple-stream tertiary "
            "leg active)",
        )
    if enabled:
        return CheckResult(
            "DTLN-aec engine", "fail",
            "JASPER_AEC_DTLN_ENABLED=1 but the running bridge could not "
            f"load the engine: {error or 'unknown error'}. Bridge "
            "degraded to AEC3-only — triple-stream is silently "
            "dual-stream and voice listens on an unfed :9878 leg. Check "
            "/var/lib/jasper/dtln/ and `journalctl -u jasper-aec-bridge -e`.",
        )
    return CheckResult(
        "DTLN-aec engine", "warn",
        "JASPER_AEC_DTLN_ENABLED=1 but the running bridge was started "
        "without the DTLN leg. If the active input profile is chip-AEC "
        "(xvf_chip_aec, or auto resolving to it), the bridge never "
        "loads DTLN — check the profile via `curl -s "
        "localhost:8780/aec` or http://jts.local/wake/. Otherwise the "
        "bridge may not have restarted since the env changed — try: "
        "sudo systemctl restart jasper-aec-bridge",
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

@doctor_check(order=54, group="aec")
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
    if _parked_as_bonded_follower():
        return CheckResult(
            "DTLN engine", "ok",
            "parked (bonded follower) — the dumb-follower profile stops "
            "voice + the AEC stack while paired; the leader owns the mic",
        )
    enabled = os.environ.get("JASPER_AEC_DTLN_ENABLED", "0").strip().lower()
    if enabled not in ("1", "true", "yes", "on"):
        return CheckResult(
            "DTLN-aec engine", "ok",
            "skipped — JASPER_AEC_DTLN_ENABLED not set (dual-stream mode)",
        )

    model_result = _check_dtln_model_assets()
    if model_result is not None:
        return model_result

    # Bridge must be running for the engine to mean anything.
    is_active = _run(
        ["systemctl", "is-active", "jasper-aec-bridge.service"]
    ).stdout.strip()
    if is_active != "active":
        return CheckResult(
            "DTLN-aec engine", "ok",
            "(bridge not running — see AEC bridge service check above)",
        )

    # Prefer the bridge's live stats snapshot — authoritative and not
    # journal-window-limited (a load failure at a bridge start >10 min
    # ago is invisible to the journal path below).
    stats_path = Path(os.environ.get(
        "JASPER_AEC_BRIDGE_STATS_PATH",
        "/run/jasper/aec_bridge_stats.json",
    ))
    try:
        stats = json.loads(stats_path.read_text())
    except (OSError, ValueError):
        stats = None
    if isinstance(stats, dict):
        result = _assess_dtln_engine_from_stats(stats, time.time())
        if result is not None:
            return result

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

def _check_dtln_model_assets() -> CheckResult | None:
    from jasper.aec_engines import dtln_models

    raw_size = os.environ.get(
        "JASPER_AEC_DTLN_SIZE", str(dtln_models.DEFAULT_SIZE)
    ).strip()
    try:
        model_size = int(raw_size)
    except ValueError:
        return CheckResult(
            "DTLN-aec engine", "fail",
            "JASPER_AEC_DTLN_ENABLED=1 but JASPER_AEC_DTLN_SIZE is not "
            f"an integer: {raw_size!r}",
        )
    model_entry = dtln_models.by_size(model_size)
    if model_entry is None:
        available = ", ".join(str(entry.size) for entry in dtln_models.REGISTRY)
        if not available:
            available = "none"
        return CheckResult(
            "DTLN-aec engine", "fail",
            "JASPER_AEC_DTLN_ENABLED=1 but JASPER_AEC_DTLN_SIZE="
            f"{model_size} is not registered in jasper/aec_engines/"
            f"dtln_models.py (available: {available})",
        )
    model_dir = Path(
        os.environ.get("JASPER_DTLN_MODEL_DIR", dtln_models.DTLN_MODELS_DIR)
    )
    missing: list[str] = []
    mismatched: list[str] = []
    for path, _, expected_sha in model_entry.files(model_dir):
        if not path.is_file() or path.stat().st_size <= 0:
            missing.append(path.name)
            continue
        if _sha256_file(path) != expected_sha:
            mismatched.append(path.name)
    if missing:
        return CheckResult(
            "DTLN-aec engine", "fail",
            "JASPER_AEC_DTLN_ENABLED=1 but model files are missing: "
            f"{', '.join(sorted(missing))} in {model_dir}; re-run deploy/install.sh",
        )
    if mismatched:
        return CheckResult(
            "DTLN-aec engine", "fail",
            "JASPER_AEC_DTLN_ENABLED=1 but model file hashes do not match "
            "the registry: "
            f"{', '.join(sorted(mismatched))} in {model_dir}; re-run deploy/install.sh",
        )
    return None

@doctor_check(order=55, group="aec")
def check_xvf_firmware_6ch() -> CheckResult:
    """6-ch firmware exposes raw mics on channels 2-5 of the XVF
    capture endpoint. The bridge depends on the 6-channel endpoint
    shape and reads channel 1 (ASR beam); channel 2 is the optional
    raw0 corpus leg."""
    from ...mics import xvf3800
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

@doctor_check(order=56, group="aec")
def check_xvf_mixer_state() -> CheckResult:
    """The XVF chip exposes each capture channel as a kernel ALSA
    mixer slot. When the chip is flashed from 2-ch to 6-ch firmware
    mid-bringup, ALSA assigns new slots for ch2-5 with defaults of
    off / 0 dB, and `alsactl restore` persists that across reboot —
    silently killing raw mics in spite of correct chip state. The
    reconciler self-heals via xvf3800.ensure_capture_open(); this
    check flags drift if anything sets them back."""
    from ...mics import xvf3800
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
