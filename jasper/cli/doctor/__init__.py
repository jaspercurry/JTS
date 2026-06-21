"""``jasper-doctor`` — preflight diagnostic CLI (package entry).

This package is the decomposed form of the original single-file
``jasper/cli/doctor.py``. The console-script entry point
(``jasper-doctor = jasper.cli.doctor:main``) and every public
name that external code or the test-suite imports resolve from
this ``__init__`` exactly as they did from the old module — the
checks were re-homed into per-domain modules
(:mod:`~jasper.cli.doctor.audio`, :mod:`~jasper.cli.doctor.network`,
…) and the cross-cutting harness/helpers into
:mod:`~jasper.cli.doctor._shared`, then re-exported here.

Usage:
    sudo /opt/jasper/.venv/bin/jasper-doctor             # one shot
    sudo /opt/jasper/.venv/bin/jasper-doctor --watch     # loop, 5s
    sudo /opt/jasper/.venv/bin/jasper-doctor --watch -i 2  # loop, 2s

The doctor reads ``/etc/jasper/jasper.env`` and (if present)
``/var/lib/jasper/voice_provider.env`` itself. Returns 0 if all
critical checks pass, 1 otherwise. --watch never returns by
itself; exits 0 on Ctrl-C.

Check membership and order are owned by the registry
(:mod:`~jasper.cli.doctor._registry`): each check is registered
with an explicit ``order=`` equal to its index in the original
hand-ordered list, and :func:`run_async` rebuilds the exact same
``DoctorCheck`` sequence from it (bare callables vs.
``(label, lambda: fn(cfg))`` tuples preserved), so displayed
order, labels, and crash-path labels are unchanged."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable, Optional
from ...config import Config
from ...env_load import load_env_files as _load_env_files
from ...install_profile import (
    STREAMBOX_INSTALL_PROFILE,
    install_profile_allows_voice_brain,
    install_role_for_profile,
    read_install_profile,
)
from ...speaker_name import runtime_name as _speaker_runtime_name
from ...usage import DEFAULT_USAGE_DB

from ._registry import doctor_check, registered_checks
from ._shared import (
    BOLD,
    CheckResult,
    DoctorCheck,
    GREEN,
    RED,
    RESET,
    YELLOW,
    _BEARER_SECRET_RE,
    _CHIP_AEC_PASSIVE_REQUIRED_CHECKS,
    _EXCEPTION_DETAIL_LIMIT,
    _KEY_VALUE_SECRET_RE,
    _KNOWN_CHIP_AEC_PASSIVE_HARDWARE,
    _RUNTIME_STATE_UNITS,
    _SECRET_PREFIX_RE,
    _active_audio_dac_env,
    _active_audio_dac_id,
    _camilla_block_field,
    _check_name,
    _crashed_check_result,
    _exception_detail,
    _loopback_playback_active,
    _meminfo_kb,
    _normalize_doctor_check,
    _parse_env_file,
    _pid_of_unit,
    _redact_exception_message,
    _run,
    _run_async_doctor_check,
    _run_doctor_check,
    _service_runtime_states,
    _sha256_file,
    _systemctl_show_property,
)
from . import env as env
from .env import (
    check_env_file,
    check_speaker_name,
    check_state_dir,
)
from . import voice as voice
from .voice import (
    _provider_api_key_attr,
    check_provider_key,
    _voice_provider_ids_manifest_path,
    check_voice_provider_ids_manifest,
    _voice_tool_packs_runtime,
    _assess_tool_packs,
    check_tool_packs,
    check_spend_cap,
    check_pricing,
)
from . import audio as audio
from .audio import (
    check_alsa_card,
    _HW_SHORTHAND_RE,
    _extract_card_name,
    _ARECORD_L_LINE_RE,
    _check_arecord_l_card_device,
    check_mic_card_matches_config,
    check_loopback,
    check_camilla_websocket,
    _jasper_voice_active,
    check_mic_capture,
    check_tts_open,
    check_output_hardware_state,
    check_apple_dongle_audio,
    check_dongle_headphone_at_max,
    check_fanin_binary_installed,
    _asound_non_comment_text,
    _asound_pcm_block,
    _FANIN_EXPECTED_INPUTS,
    _FANIN_EXPECTED_OUTPUT_PCM,
    _OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM,
    _OUTPUTD_EXPECTED_CONTENT_PCM,
    _OUTPUTD_EXPECTED_DAC_PCM,
    _OUTPUTD_EXPECTED_DUAL_DAC_PCM,
    _OUTPUTD_STATUS_SOCKET,
    check_fanin_asound_wiring,
    check_fanin_service,
    check_fanin_tts_drops,
    check_outputd_service,
    check_aec_clock_drift,
    _devices_volume_limit_from_text,
    check_camilla_volume_limit,
    check_active_speaker_runtime_graph,
    _sound_profile_path,
    check_sound_profile,
    check_dsp_apply_state,
)
from . import wake as wake
from .wake import (
    check_openwakeword_model,
    _voice_wake_legs_runtime,
    _assess_wake_legs,
    check_wake_legs_configured,
)
from . import renderers as renderers
from .renderers import (
    check_librespot_running,
    check_shairport_sync_ap2,
    check_nqptp_running,
    check_jasper_mux,
    check_bluealsa,
    check_bluetooth_pairing_policy,
    check_spotify_cache,
    check_spotify_connect_device,
    check_shairport_sync_loopback_plughw,
    _read_first_line_matching,
    _renderer_device_shairport,
    _renderer_device_librespot,
    _renderer_device_bluealsa,
    _systemd_user_for,
    _resolve_systemd_env_vars,
    _probe_open_as_user,
    _FANIN_PRIVATE_RENDERER_DEVICES,
    _alsa_busy,
    _fanin_lane_busy_owner_matches,
    check_renderer_device_resolvable,
)
from . import integrations as integrations
from .integrations import (
    check_google_tokens,
    check_home_assistant,
    check_citibike,
)
from . import memory as memory
from .memory import (
    check_ram,
    check_memory_headroom,
    check_zram_size_ratio,
    check_mglru_min_ttl,
    _JTS_SYSCTL_CONF,
    _SysctlConf,
    _parse_jts_sysctl_conf,
    check_sysctl_drift,
    _EXPECTED_OOM_ADJ,
    check_oom_score_adj,
    check_cgroup_memory_enabled,
    _AUDIO_PATH_UNITS,
    _AUDIO_VMSWAP_WARN_KB,
    check_audio_path_no_swap,
    _DEFAULT_DISK_WARN_PERCENT,
    _DISK_FAIL_PERCENT,
    _disk_warn_percent,
    check_disk_space,
    _STORAGE_WALK_MAX_ENTRIES,
    _STORAGE_WALK_MAX_DEPTH,
    _bounded_dir_size,
    check_correction_storage,
    check_wake_events_storage,
)
from . import resilience as resilience
from .resilience import (
    _EXPECTED_START_LIMIT_ACTION,
    check_start_limit_action,
    check_service_runtime_state,
    check_bootloop_guard,
)
from . import aec as aec
from .aec import (
    _aec_mode_setting,
    _aec_profile_setting,
    _wake_leg_setting,
    _chip_aec_available_for_doctor,
    _audio_profile_status_for_doctor,
    _assess_audio_profile,
    check_audio_profile_runtime,
    _assess_audio_validation_summary,
    _known_supported_chip_aec_passive_ok,
    check_audio_validation_readiness,
    check_aec_bridge_running,
    _AEC_RMS_RE,
    _AEC_MIC_MUSIC_THRESHOLD,
    _AEC_REF_SILENT_THRESHOLD,
    _AEC_DRIFT_WARN_THRESHOLD,
    _assess_aec_bridge_output,
    check_aec_bridge_output_health,
    _assess_dtln_engine,
    check_aec_bridge_dtln_engine,
    _check_dtln_model_assets,
    check_xvf_firmware_6ch,
    check_xvf_mixer_state,
)
from . import usbsink as usbsink
from .usbsink import (
    _systemd_is_active,
    _module_loaded,
    check_usbsink_dtoverlay,
    check_usbsink_state,
    check_usbsink_card,
    check_usbsink_name,
    check_usbsink_active_libcomposite,
    check_usbsink_preempt_port_reachable,
)
from . import network as network
from .network import (
    _parse_iw_regdom,
    _format_phy_regdom_detail,
    check_wifi_regdom,
    check_wifi_guardian,
    check_wifi_recover_timer,
    check_avahi_daemon,
    check_hostname_avahi_consistency,
    check_avahi_jasper_control,
)
from . import correction as correction
from .correction import (
    _correction_root,
    check_correction_web_service,
    _probe_https_status,
    check_correction_https_assets,
    check_correction_state_dirs,
    _parse_camilla_statefile_config_path,
    _active_camilla_config_path,
    check_correction_current_config,
    _format_byte_count,
    _correction_evidence_status,
    check_correction_latest_bundle,
)
from . import web as web
from .web import (
    check_conversation_history,
    check_control_token,
    check_web_design_assets,
)
from . import peering as peering
from .peering import (
    check_peering_mode,
    check_peering_discovery,
    _local_peer_id,
)
from . import grouping as grouping
from .grouping import (
    check_grouping,
    check_grouping_channel_pick,
    check_grouping_household_credential,
    check_grouping_leader_pipe,
    check_grouping_pair_channels,
    check_grouping_tts_lane,
    check_grouping_rate_adjust,
)
from . import satellites as satellites
from .satellites import (
    check_dial_heartbeat,
)

_STREAMBOX_OMITTED_DOCTOR_GROUPS = frozenset({
    "voice",
    "wake",
    "integrations",
    "aec",
    "satellites",
})

_STREAMBOX_OMITTED_DOCTOR_CHECKS = frozenset({
    "check_mic_card_matches_config",
    "check_mic_capture",
    "check_tts_open",
})


def _profile_skip_result(entry, *, reason: str) -> CheckResult:
    label = entry.label or _check_name(entry.func)
    return CheckResult(label, "ok", reason)


def _doctor_skip_reason(entry, install_profile: str) -> str:
    role = install_role_for_profile(install_profile)
    if role == STREAMBOX_INSTALL_PROFILE and (
        entry.group in _STREAMBOX_OMITTED_DOCTOR_GROUPS
        or entry.func.__name__ in _STREAMBOX_OMITTED_DOCTOR_CHECKS
    ):
        return "not installed (streambox profile)"
    return ""

__all__ = [
    "doctor_check",
    "registered_checks",
    "Config",
    "_load_env_files",
    "BOLD",
    "CheckResult",
    "DoctorCheck",
    "GREEN",
    "RED",
    "RESET",
    "YELLOW",
    "_BEARER_SECRET_RE",
    "_CHIP_AEC_PASSIVE_REQUIRED_CHECKS",
    "_EXCEPTION_DETAIL_LIMIT",
    "_KEY_VALUE_SECRET_RE",
    "_KNOWN_CHIP_AEC_PASSIVE_HARDWARE",
    "_RUNTIME_STATE_UNITS",
    "_SECRET_PREFIX_RE",
    "_active_audio_dac_env",
    "_active_audio_dac_id",
    "_camilla_block_field",
    "_check_name",
    "_crashed_check_result",
    "_exception_detail",
    "_loopback_playback_active",
    "_meminfo_kb",
    "_normalize_doctor_check",
    "_parse_env_file",
    "_pid_of_unit",
    "_redact_exception_message",
    "_run",
    "_run_async_doctor_check",
    "_run_doctor_check",
    "_service_runtime_states",
    "_sha256_file",
    "_systemctl_show_property",
    "env",
    "voice",
    "audio",
    "wake",
    "renderers",
    "integrations",
    "memory",
    "resilience",
    "aec",
    "usbsink",
    "network",
    "correction",
    "web",
    "peering",
    "grouping",
    "satellites",
    "check_env_file",
    "check_speaker_name",
    "check_state_dir",
    "_provider_api_key_attr",
    "check_provider_key",
    "_voice_provider_ids_manifest_path",
    "check_voice_provider_ids_manifest",
    "_voice_tool_packs_runtime",
    "_assess_tool_packs",
    "check_tool_packs",
    "check_spend_cap",
    "check_pricing",
    "check_alsa_card",
    "_HW_SHORTHAND_RE",
    "_extract_card_name",
    "_ARECORD_L_LINE_RE",
    "_check_arecord_l_card_device",
    "check_mic_card_matches_config",
    "check_loopback",
    "check_camilla_websocket",
    "_jasper_voice_active",
    "check_mic_capture",
    "check_tts_open",
    "check_output_hardware_state",
    "check_apple_dongle_audio",
    "check_dongle_headphone_at_max",
    "check_fanin_binary_installed",
    "_asound_non_comment_text",
    "_asound_pcm_block",
    "_FANIN_EXPECTED_INPUTS",
    "_FANIN_EXPECTED_OUTPUT_PCM",
    "_OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM",
    "_OUTPUTD_EXPECTED_CONTENT_PCM",
    "_OUTPUTD_EXPECTED_DAC_PCM",
    "_OUTPUTD_EXPECTED_DUAL_DAC_PCM",
    "_OUTPUTD_STATUS_SOCKET",
    "check_fanin_asound_wiring",
    "check_fanin_service",
    "check_fanin_tts_drops",
    "check_outputd_service",
    "check_aec_clock_drift",
    "_devices_volume_limit_from_text",
    "check_camilla_volume_limit",
    "check_active_speaker_runtime_graph",
    "_sound_profile_path",
    "check_sound_profile",
    "check_dsp_apply_state",
    "check_openwakeword_model",
    "_voice_wake_legs_runtime",
    "_assess_wake_legs",
    "check_wake_legs_configured",
    "check_librespot_running",
    "check_shairport_sync_ap2",
    "check_nqptp_running",
    "check_jasper_mux",
    "check_bluealsa",
    "check_bluetooth_pairing_policy",
    "check_spotify_cache",
    "check_spotify_connect_device",
    "check_shairport_sync_loopback_plughw",
    "_read_first_line_matching",
    "_renderer_device_shairport",
    "_renderer_device_librespot",
    "_renderer_device_bluealsa",
    "_systemd_user_for",
    "_resolve_systemd_env_vars",
    "_probe_open_as_user",
    "_FANIN_PRIVATE_RENDERER_DEVICES",
    "_alsa_busy",
    "_fanin_lane_busy_owner_matches",
    "check_renderer_device_resolvable",
    "check_google_tokens",
    "check_home_assistant",
    "check_citibike",
    "check_ram",
    "check_memory_headroom",
    "check_zram_size_ratio",
    "check_mglru_min_ttl",
    "_JTS_SYSCTL_CONF",
    "_SysctlConf",
    "_parse_jts_sysctl_conf",
    "check_sysctl_drift",
    "_EXPECTED_OOM_ADJ",
    "check_oom_score_adj",
    "check_cgroup_memory_enabled",
    "_AUDIO_PATH_UNITS",
    "_AUDIO_VMSWAP_WARN_KB",
    "check_audio_path_no_swap",
    "_DEFAULT_DISK_WARN_PERCENT",
    "_DISK_FAIL_PERCENT",
    "_disk_warn_percent",
    "check_disk_space",
    "_STORAGE_WALK_MAX_ENTRIES",
    "_STORAGE_WALK_MAX_DEPTH",
    "_bounded_dir_size",
    "check_correction_storage",
    "check_wake_events_storage",
    "_EXPECTED_START_LIMIT_ACTION",
    "check_start_limit_action",
    "check_service_runtime_state",
    "check_bootloop_guard",
    "_aec_mode_setting",
    "_aec_profile_setting",
    "_wake_leg_setting",
    "_chip_aec_available_for_doctor",
    "_audio_profile_status_for_doctor",
    "_assess_audio_profile",
    "check_audio_profile_runtime",
    "_assess_audio_validation_summary",
    "_known_supported_chip_aec_passive_ok",
    "check_audio_validation_readiness",
    "check_aec_bridge_running",
    "_AEC_RMS_RE",
    "_AEC_MIC_MUSIC_THRESHOLD",
    "_AEC_REF_SILENT_THRESHOLD",
    "_AEC_DRIFT_WARN_THRESHOLD",
    "_assess_aec_bridge_output",
    "check_aec_bridge_output_health",
    "_assess_dtln_engine",
    "check_aec_bridge_dtln_engine",
    "_check_dtln_model_assets",
    "check_xvf_firmware_6ch",
    "check_xvf_mixer_state",
    "_systemd_is_active",
    "_module_loaded",
    "check_usbsink_dtoverlay",
    "check_usbsink_state",
    "check_usbsink_card",
    "check_usbsink_name",
    "check_usbsink_active_libcomposite",
    "check_usbsink_preempt_port_reachable",
    "_parse_iw_regdom",
    "_format_phy_regdom_detail",
    "check_wifi_regdom",
    "check_wifi_guardian",
    "check_wifi_recover_timer",
    "check_avahi_daemon",
    "check_hostname_avahi_consistency",
    "check_avahi_jasper_control",
    "_correction_root",
    "check_correction_web_service",
    "_probe_https_status",
    "check_correction_https_assets",
    "check_correction_state_dirs",
    "_parse_camilla_statefile_config_path",
    "_active_camilla_config_path",
    "check_correction_current_config",
    "_format_byte_count",
    "_correction_evidence_status",
    "check_correction_latest_bundle",
    "check_conversation_history",
    "check_web_design_assets",
    "check_control_token",
    "check_peering_mode",
    "check_peering_discovery",
    "_local_peer_id",
    "check_grouping",
    "check_grouping_channel_pick",
    "check_grouping_household_credential",
    "check_grouping_leader_pipe",
    "check_grouping_pair_channels",
    "check_grouping_tts_lane",
    "check_grouping_rate_adjust",
    "check_dial_heartbeat",
    "_PROBE_REF_PASS_THRESHOLD",
    "_PROBE_SINE_PATH",
    "_PROBE_SINE_DURATION_S",
    "probe_aec_ref_path",
    "render",
    "render_json",
    "_watch_line",
    "_watch_loop",
    "_build_doctor_checks",
    "_doctor_check_timeout",
    "_doctor_max_concurrency",
    "main",
    "run_async",
    "argparse",
    "asyncio",
    "hashlib",
    "json",
    "os",
    "re",
    "shutil",
    "socket",
    "subprocess",
    "sys",
    "time",
    "Path",
    "Awaitable",
    "Callable",
    "Optional",
]

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
    import wave

    from ...control import client as control

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
        state = control.get_state(timeout=3)
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
    except (control.ControlError, json.JSONDecodeError) as e:
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

def _json_payload(
    results: list[CheckResult],
    *,
    duration_sec: float | None = None,
) -> dict:
    """The flat /system-dashboard schema — one row per check."""
    payload = {
        "fails": sum(1 for r in results if r.status == "fail"),
        "warns": sum(1 for r in results if r.status == "warn"),
        "generated_at_epoch": time.time(),
        "results": [
            {"name": r.name, "status": r.status, "detail": r.detail}
            for r in results
        ],
    }
    if duration_sec is not None:
        payload["duration_sec"] = round(duration_sec, 3)
    return payload


def _emit_json(payload: dict, out_path: str | None) -> None:
    """Emit the JSON report to stdout, or atomically to ``out_path``.

    ``--out`` is how the non-root jasper-control gets a ROOT-fidelity report
    (WS1 Phase 3b-2): a root ``jasper-doctor-json.service`` oneshot writes here
    and jasper-control serves the file at /system/diagnostics. 0640 so the
    `jasper` group (jasper-control's primary group; the oneshot runs root:jasper)
    can read it without it being world-readable."""
    import json as _json
    text = _json.dumps(payload)
    if out_path is None:
        print(text)
        return
    from jasper.atomic_io import atomic_write_text
    atomic_write_text(out_path, text + "\n", mode=0o640)


def render_json(
    results: list[CheckResult],
    out_path: str | None = None,
    *,
    duration_sec: float | None = None,
) -> int:
    """Machine-readable output for the /system dashboard.

    The web UI fetches this via /system/diagnostics → jasper-control. Returns
    text-render exit semantics (0 = ok/warn only; 1 = a fail) on the stdout
    path. With ``out_path`` (the dashboard-capture oneshot), the report lands
    in the file and we return 0 — the file carries the pass/fail, and a
    non-zero exit would needlessly flip the oneshot to ``failed``.

    Schema is intentionally flat — one row per check — so the dashboard can
    render a table without complex per-check logic."""
    payload = _json_payload(results, duration_sec=duration_sec)
    _emit_json(payload, out_path)
    if out_path is not None:
        return 0
    return 1 if payload["fails"] else 0

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

def _env_int_for_doctor(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def _env_float_for_doctor(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


_DOCTOR_DEFAULT_CONCURRENCY = 8
_DOCTOR_MAX_CONCURRENCY = 16
_DOCTOR_DEFAULT_CHECK_TIMEOUT_SECONDS = 15.0


def _doctor_max_concurrency() -> int:
    return max(
        1,
        min(
            _DOCTOR_MAX_CONCURRENCY,
            _env_int_for_doctor(
                "JASPER_DOCTOR_MAX_CONCURRENCY",
                _DOCTOR_DEFAULT_CONCURRENCY,
            ),
        ),
    )


def _doctor_check_timeout() -> float:
    return _env_float_for_doctor(
        "JASPER_DOCTOR_CHECK_TIMEOUT_SECONDS",
        _DOCTOR_DEFAULT_CHECK_TIMEOUT_SECONDS,
    )


def _local_audio_config_from_env() -> SimpleNamespace:
    """Cfg surface for profiles that run local audio without a voice brain.

    Streambox installs intentionally do not require a
    voice provider. Keep this namespace to the attributes retained doctor
    checks actually read, so jasper-doctor can still validate local audio,
    renderer, correction, memory, network, and web health without pulling
    the full voice Config into small-device profiles.
    """
    hostname = os.environ.get("JASPER_HOSTNAME", "jts.local") or "jts.local"
    spotify_redirect_default = (
        "https://jaspercurry.github.io/spotify-oauth-callback/"
        f"?host={hostname}"
    )
    spotify_client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    return SimpleNamespace(
        usage_db=os.environ.get("JASPER_USAGE_DB", DEFAULT_USAGE_DB),
        camilla_host=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
        camilla_port=_env_int_for_doctor("JASPER_CAMILLA_PORT", 1234),
        spotify_client_id=spotify_client_id,
        spotify_redirect_uri=(
            os.environ.get("SPOTIFY_REDIRECT_URI", spotify_redirect_default)
            or spotify_redirect_default
        ),
        spotify_cache_path=os.environ.get(
            "SPOTIFY_CACHE_PATH",
            "/var/lib/jasper-intsecrets/.spotify-cache",
        ),
        spotify_device_name=_speaker_runtime_name(),
        spotify_accounts_path=os.environ.get(
            "JASPER_SPOTIFY_ACCOUNTS_PATH",
            "/var/lib/jasper-intsecrets/spotify/accounts.json",
        ),
        spotify_setup_url=os.environ.get(
            "JASPER_SPOTIFY_SETUP_URL",
            f"http://{hostname}/spotify",
        ),
        spotify_enabled=bool(spotify_client_id),
    )

def _doctor_config_from_env(install_profile: str) -> Config | SimpleNamespace:
    if not install_profile_allows_voice_brain(install_profile):
        return _local_audio_config_from_env()
    return Config.from_env()

async def _watch_loop(cfg: Config | SimpleNamespace, interval: float) -> int:
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


@dataclass(frozen=True)
class _RunnableDoctorCheck:
    name: str
    check: DoctorCheck | Callable[[], Awaitable[CheckResult]]
    is_async: bool = False
    exclusive_group: str = ""


def _build_doctor_checks(
    cfg: Config | SimpleNamespace,
    install_profile: str,
) -> list[_RunnableDoctorCheck]:
    checks: list[_RunnableDoctorCheck] = []
    for entry in registered_checks():
        skip_reason = _doctor_skip_reason(entry, install_profile)
        if skip_reason:
            skipped = _profile_skip_result(entry, reason=skip_reason)
            check = (skipped.name, lambda skipped=skipped: skipped)
            checks.append(_RunnableDoctorCheck(skipped.name, check))
            continue
        fn = entry.func
        if entry.is_async:
            name = entry.label or _check_name(fn)  # type: ignore[arg-type]
            async_check = (
                (lambda fn=fn, cfg=cfg: fn(cfg))
                if entry.needs_cfg
                else (lambda fn=fn: fn())
            )
            checks.append(
                _RunnableDoctorCheck(
                    name,
                    async_check,  # type: ignore[arg-type]
                    is_async=True,
                    exclusive_group=entry.exclusive_group,
                )
            )
            continue
        if entry.needs_cfg:
            check: DoctorCheck = (entry.label, lambda fn=fn, cfg=cfg: fn(cfg))
        else:
            check = fn  # type: ignore[assignment]
        name, _ = _normalize_doctor_check(check)
        checks.append(
            _RunnableDoctorCheck(
                name,
                check,
                exclusive_group=entry.exclusive_group,
            )
        )
    return checks


async def _run_runnable_doctor_check(
    runnable: _RunnableDoctorCheck,
) -> CheckResult:
    if runnable.is_async:
        return await _run_async_doctor_check(
            runnable.name,
            runnable.check,  # type: ignore[arg-type]
        )
    return await asyncio.to_thread(
        _run_doctor_check,
        runnable.check,  # type: ignore[arg-type]
    )


async def _run_runnable_with_timeout(
    runnable: _RunnableDoctorCheck,
    timeout: float,
) -> CheckResult:
    # This is an outer row-level guard. `asyncio.to_thread` cannot kill a
    # worker that is already inside a blocking syscall, and asyncio will wait
    # for default-executor threads during shutdown. Keep individual blocking
    # probes bounded with their own subprocess/socket timeouts too.
    try:
        return await asyncio.wait_for(
            _run_runnable_doctor_check(runnable),
            timeout=timeout,
        )
    except TimeoutError:
        return CheckResult(
            runnable.name,
            "fail",
            f"check timed out after {timeout:g}s",
        )


async def _run_runnable_bounded(
    runnable: _RunnableDoctorCheck,
    semaphore: asyncio.Semaphore,
    exclusive_locks: dict[str, asyncio.Lock],
    timeout: float,
) -> CheckResult:
    async with semaphore:
        if runnable.exclusive_group:
            async with exclusive_locks[runnable.exclusive_group]:
                return await _run_runnable_with_timeout(runnable, timeout)
        return await _run_runnable_with_timeout(runnable, timeout)


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
        "--out", metavar="PATH", default=None,
        help="With --json, write the report atomically to PATH (0640) instead "
             "of stdout, then exit 0. The root jasper-doctor-json.service "
             "oneshot uses this so the non-root jasper-control can serve a "
             "root-fidelity report at /system/diagnostics (WS1 Phase 3b-2).",
    )
    parser.add_argument(
        "--probe-aec", action="store_true",
        help="Active probe — play a brief sine into correction_substream "
             "and verify the AEC bridge's `ref` rises in its rms log. "
             "Skips the standard checks and runs only this one test. "
             "Refuses if a renderer is currently playing.",
    )
    args = parser.parse_args()
    # --out is a JSON-to-file capture; it implies --json so the oneshot can
    # pass `--json --out PATH` (or just `--out PATH`) and always get a report.
    if args.out:
        args.json = True
    _load_env_files()
    try:
        install_profile = read_install_profile()
        cfg = _doctor_config_from_env(install_profile)
    except (RuntimeError, ValueError) as e:
        if args.json:
            _emit_json(
                {"error": f"config: {e}", "fails": 1, "warns": 0, "results": []},
                args.out,
            )
            sys.exit(0 if args.out else 1)
        print(f"{RED}config error: {e}{RESET}", file=sys.stderr)
        sys.exit(1)
    if args.probe_aec:
        results = probe_aec_ref_path()
        if args.json:
            sys.exit(render_json(results, out_path=args.out))
        sys.exit(render(results))
    if args.watch:
        sys.exit(asyncio.run(_watch_loop(cfg, args.interval)))
    started_at = time.monotonic()
    try:
        results = asyncio.run(run_async(cfg))
    except Exception as e:  # noqa: BLE001
        if args.json:
            detail = _exception_detail(e)
            _emit_json(
                {
                    "error": f"doctor crashed: {detail}",
                    "fails": 1,
                    "warns": 0,
                    "results": [{
                        "name": "jasper-doctor",
                        "status": "fail",
                        "detail": detail,
                    }],
                },
                args.out,
            )
            sys.exit(0 if args.out else 1)
        raise
    if args.json:
        sys.exit(render_json(
            results,
            out_path=args.out,
            duration_sec=time.monotonic() - started_at,
        ))
    sys.exit(render(results))

if __name__ == "__main__":
    main()

async def run_async(cfg: Config | SimpleNamespace) -> list[CheckResult]:
    """Run every registered check in canonical order and return the results.

    The registry remains the ordering source of truth. Checks run
    concurrently because most are subprocess/socket/file probes, but
    results are gathered in registry order so CLI and dashboard output
    stay stable. ``exclusive_group=`` registry metadata serializes
    hardware-sensitive probes within that lane while unrelated checks
    continue.
    """
    install_profile = read_install_profile()
    checks = _build_doctor_checks(cfg, install_profile)
    semaphore = asyncio.Semaphore(_doctor_max_concurrency())
    exclusive_locks = {
        c.exclusive_group: asyncio.Lock()
        for c in checks
        if c.exclusive_group
    }
    timeout = _doctor_check_timeout()
    return list(await asyncio.gather(*[
        _run_runnable_bounded(c, semaphore, exclusive_locks, timeout)
        for c in checks
    ]))
