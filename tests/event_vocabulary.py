# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Frozen `event=` vocabulary snapshots read by test_log_event_conventions.

Three exception tables, each recorded so the guard fails on a NEW deviation
while the ones already in the tree are worked off. None of them may grow.
"""
from __future__ import annotations

# Event names with no `domain.action` dot — the sole exception to the shape
# check. Most sit in the parked tuning zone (jasper/audio_measurement/,
# jasper/active_speaker/), whose renames wait on unpark.
# Removal condition: shrinks as sites are renamed; delete when empty.
FLAT_EVENT_NAMES: tuple[str, ...] = (
    "active_speaker_baseline_config_written",
    "active_speaker_commissioning_config_written",
    "active_speaker_driver_domain_config_written",
    "active_speaker_program_bake_config_written",
    "active_speaker_program_config_written",
    "active_speaker_startup_config_written",
    "correction_bundle_dependency_ignored",
    "correction_bundle_manifest_entry_dropped",
    "correction_bundle_manifest_reset",
    "correction_calibration_lookup",
    "correction_calibration_sign_migrated",
    "correction_calibration_sign_migration",
    "level_feed_stream_reset",
    "level_lock_stored",
    "level_match_done",
    "ramp_agc_indeterminate",
    "ramp_agc_marginal",
    "ramp_agc_suspected",
    "ramp_agc_verified",
    "ramp_cancelled",
    "ramp_cap_settling",
    "ramp_clip_abort",
    "ramp_env_config_invalid",
    "ramp_error",
    "ramp_feed_lost",
    "ramp_locked",
    "ramp_maxed_out",
    "ramp_noise_floor_invalid",
    "ramp_pre_window",
    "ramp_safety_timeout",
    "ramp_settle_jump",
    "ramp_settled",
    "ramp_start",
    "ramp_volume_restored",
)

# Top-level event prefixes emitted from more than one package, mapped to the
# packages that emit them (`jasper` is the top-level modules). The guard fails
# when a prefix reaches a package this table does not list, and when a listed
# package stops emitting it.
# Removal condition: drop an entry when its prefix has one owner again; delete
# the table when it is empty.
PREFIX_OWNERS: dict[str, tuple[str, ...]] = {
    "active_speaker": ("active_speaker", "cli"),
    "aec": ("aec", "control"),
    "aec_bridge": ("aec", "cli"),
    "airplay": ("jasper", "web"),
    "assistant_loudness": ("jasper", "voice"),
    "barge": ("jasper", "voice"),
    "bluetooth": ("bluetooth", "jasper", "web"),
    "correction": ("active_speaker", "audio_measurement", "jasper", "web"),
    "cue": ("cues", "voice"),
    "dsp": ("active_speaker", "jasper"),
    "ha": ("control", "jasper", "tools", "web"),
    "household_credential": ("control", "web"),
    "http": ("control", "web"),
    "install_profile": ("control", "jasper"),
    "local_sources": ("jasper", "local_sources"),
    "manual_mic": ("jasper", "voice"),
    "measurement": ("control", "voice"),
    "mic": ("control", "jasper"),
    "peering": ("control", "jasper", "peering"),
    "pricing": ("voice", "web"),
    "research": ("research", "voice"),
    "session": ("jasper", "voice"),
    "sound": ("cli", "sound", "web"),
    "source": ("control", "jasper"),
    "spotify": ("jasper", "voice", "web"),
    "transit": ("jasper", "tools", "transit", "web"),
    "tts_flush": ("jasper", "voice"),
    "tts_write": ("jasper",),
    "turn": ("jasper", "voice"),
    "usb_mic": ("aec", "cli", "control"),
    "usbsink": ("jasper", "usbsink"),
    "voice": ("jasper", "voice", "web"),
    "volume": ("control", "jasper", "tools"),
    "wake": ("control", "jasper", "voice", "web"),
    "wake_corpus": ("wake_corpus", "web"),
    "weather": ("jasper", "web"),
}

# Event names a jasper/ reader names but no jasper/ site emits: `usbsink_name.*`
# comes from deploy/usbsink/jasper-usbsink-name-patch and `fanin.ring.opened`
# from rust/jasper-fanin, both invisible to a Python AST collector.
# Removal condition: drop an entry when its emitter moves into jasper/, or when
# the collector learns to read rust/ and deploy/ too.
CONSUMED_ELSEWHERE: tuple[str, ...] = (
    "fanin.ring.opened",
    "usbsink_name",
)
