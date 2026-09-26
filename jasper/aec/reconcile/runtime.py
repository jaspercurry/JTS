# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Apply one observed mic/AEC state, preserving service handover ordering."""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from jasper.aec.reconcile import VOICE_RESTART_INTENT_MARKER
from jasper.aec.reconcile.observe import card_id, observe
from jasper.aec_ready import aec_bridge_ready_marker_path
from jasper.atomic_io import atomic_write_json, locked_upsert_env_file
from jasper.audio_profile_state import (
    WAKE_LEG_DEFAULTS, infer_audio_input_profile, intent_from_env,
    normalize_aec_mode, normalize_audio_input_profile, parse_env_bool,
    resolve_profile_wake_legs,
)
from jasper.chip_aec.health import AlignmentHealth, alignment_health
from jasper.env_file import parse_env_mapping, quote_env_value, read_env_file, read_env_file_text
from jasper.mics import xvf3800
from jasper.service_units import SYSTEMCTL_TIMEOUT_SEC, run_systemctl
from jasper.voice.input_presence import voice_input_absent_marker_path


VOICE_IRRELEVANT_ENV_KEYS = frozenset({
    "JASPER_XVF_DISPLAY_NAME", "JASPER_XVF_REASON", "JASPER_XVF_RECOMMENDED_PROFILE",
    "JASPER_AEC_CHIP_AEC_TESTING_REQUESTED", "JASPER_AEC_CHIP_AEC_DAC_ID",
    "JASPER_AEC_CHIP_AEC_DAC_STATUS", "JASPER_AEC_CHIP_AEC_DAC_SOURCE",
    "JASPER_AEC_CHIP_AEC_DAC_DETAIL", "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS",
    "JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON", "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION",
    "JASPER_AEC_CHIP_AEC_ALIGNMENT_SELECTION",
})
XVF_2CH_DISCLOSURE = "echo cancellation unavailable until DFU flash to 6-channel firmware"
XVF_2CH_ACTION = "Re-flash the XVF to 6-channel firmware; docs/bringup.md 'XVF firmware: switch to 6-channel variant via DFU' has the procedure"


def _path(key: str, default: str) -> Path:
    return Path(os.environ.get(key, default))


def _text(path: Path) -> str:
    text, _error = read_env_file_text(path)
    return (text or "").rstrip("\n")


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _quote(value: str) -> str:
    return shlex.quote(value) if "'" not in value else quote_env_value(value)


class Reconcile:
    def __init__(self, reason: str):
        self.reason = reason
        self.env_path = _path("JASPER_ENV_FILE", "/etc/jasper/jasper.env")
        self.mode_path = _path("JASPER_AEC_MODE_FILE", "/var/lib/jasper/aec_mode.env")
        self.absent_marker = Path(voice_input_absent_marker_path())
        self.ready_marker = Path(aec_bridge_ready_marker_path())
        self.alignment_path = _path("JASPER_AEC_ALIGNMENT_RECORD_FILE", "/run/jasper-aec-init/alignment")
        self.restart_stamp = _path("JASPER_VOICE_RESTART_STAMP", "/run/jasper-aec-reconcile/voice-restart.stamp")
        self.restart_intent = _path("JASPER_VOICE_RESTART_INTENT_MARKER", VOICE_RESTART_INTENT_MARKER)
        self.group_path = _path("JASPER_GROUPING_VOICE_ENV_FILE", "/var/lib/jasper/grouping-voice.env")
        self.want_dropin = _path("JASPER_SYSTEMD_DIR", "/etc/systemd/system") / "jasper-voice.service.d/10-aec-bridge-want.conf"
        self.systemctl = os.environ.get("JASPER_SYSTEMCTL", "systemctl")
        self.start_env = read_env_file(self.env_path)
        self.values = dict(os.environ) | self.start_env
        self.installed_build = _text(_path("JASPER_INSTALL_MANIFEST", "/var/lib/jasper/build.txt"))
        self.group_text, self.group_error = read_env_file_text(self.group_path)
        self.voice_restart_needed = False
        self.outputd_restart_needed = False
        self.accessory_restart_needed = False
        self.arm = False

    def log(self, text: str) -> None:
        print(f"jasper-aec-reconcile[{self.reason}]: {text}", file=sys.stderr)

    def system(self, *args: str) -> bool:
        # Init can take ~55s. Lifecycle waits retain reconcile.service/commission
        # caller deadlines; manual calls intentionally have no Python deadline.
        timeout = SYSTEMCTL_TIMEOUT_SEC if args[0] in {"is-active", "is-enabled", "reset-failed", "--no-block"} else None
        try:
            return run_systemctl(args, executable=self.systemctl, capture_output=False, timeout=timeout).returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.log(f"event=aec_reconcile.systemctl status=failed error={exc}")
            return False

    def reset(self, *units: str) -> None:
        for unit in units:
            self.system("reset-failed", unit)

    def active_enabled(self, unit: str) -> bool:
        return self.system("is-active", "--quiet", unit) and self.system("is-enabled", "--quiet", unit)

    def publish(self, updates: dict[str, str]) -> None:
        for key, value in updates.items():
            if key not in VOICE_IRRELEVANT_ENV_KEYS and self.start_env.get(key, "") != value:
                self.voice_restart_needed = True
        locked_upsert_env_file(
            self.env_path, lambda _text: ((key, _quote(value)) for key, value in updates.items()),
            mode=0o640, dir_mode=0o755,
        )
        self.values.update(updates)

    def seed_mode(self) -> None:
        def seeds(text: str):
            values = parse_env_mapping(text)
            defaults = {key: str(int(default)) for _, key, default in WAKE_LEG_DEFAULTS}
            defaults["JASPER_AEC_CHIP_REF_OBSERVE"] = "0"
            if self.mode_path.exists():
                intent = replace(intent_from_env(values), mode=values.get("JASPER_AEC_MODE", ""))
                defaults["JASPER_AUDIO_INPUT_PROFILE"] = infer_audio_input_profile(intent)
            else:
                defaults.update(JASPER_AEC_MODE="auto", JASPER_AUDIO_INPUT_PROFILE="auto")
            return ((key, value) for key, value in defaults.items() if key not in values)
        try:
            locked_upsert_env_file(self.mode_path, seeds, mode=0o644, dir_mode=0o755)
        except TimeoutError as exc:
            self.log(f"event=aec_reconcile.mode_seed status=unavailable error={exc}")

    def commission_active(self) -> bool:
        marker = _path("JASPER_AEC_COMMISSION_MARKER", "/run/jasper-chip-aec-commission/active")
        match = re.fullmatch(r"pid=([1-9][0-9]*)", _text(marker).split("\n")[0])
        if match and (_path("JASPER_PROC_ROOT", "/proc") / match[1]).is_dir():
            self.arm = self.reason == "chip-aec-commission-arm"
            return True
        _unlink(marker)
        return False

    def ready(self, publish: bool) -> None:
        if publish:
            try:
                self.ready_marker.parent.mkdir(parents=True, exist_ok=True)
                self.ready_marker.write_text(f"reason={self.reason}\n")
            except OSError:
                self.log(f"event=aec_reconcile.aec_ready_marker_write_failed marker={self.ready_marker}")
                return
        else:
            _unlink(self.ready_marker)
        self.log(f"event=aec_reconcile.bridge_ready state={'published' if publish else 'revoked'}")

    def mark_absent(self, code: str, detail: str) -> None:
        try:
            self.absent_marker.write_text(f"reason={code}\ndetail={detail}\n")
            self.absent_marker.chmod(0o644)
        except OSError:
            self.log(f"event=aec_reconcile.voice_input_marker_write_failed marker={self.absent_marker} reason={code}")
            self.system("disable", "jasper-voice.service")
            return
        self.log(f"event=aec_reconcile.voice_input_absent state=marked reason={code} detail={detail}")

    def clear_absent(self) -> None:
        if self.absent_marker.exists():
            reason = _text(self.absent_marker).split("\n")[0].removeprefix("reason=")
            _unlink(self.absent_marker)
            self.log(f"event=aec_reconcile.voice_input_absent state=cleared reason={reason}")
        self.system("enable", "jasper-voice.service")

    def voice_inputs(self) -> str:
        return f"{self.installed_build}\naccessory_mic_sources={','.join(self.facts.accessory_sources)}\ngrouping_voice_env:\n{(self.group_text or '').rstrip(chr(10))}"

    def voice_skip(self) -> bool:
        return (
            not self.voice_restart_needed and not self.absent_marker.exists()
            and self.facts.accessory_status == "resolved" and self.group_error is None
            and bool(_text(self.restart_stamp))
            and _text(self.restart_stamp) == self.voice_inputs().rstrip("\n")
            and self.active_enabled("jasper-voice.service")
        )

    def aec_skip(self) -> bool:
        return (self.active_enabled("jasper-aec-init.service")
                and self.active_enabled("jasper-aec-bridge.service") and self.voice_skip())

    def restart_voice(self) -> None:
        if self.voice_skip():
            self.log(f"event=aec_reconcile.voice_restart_skipped reason=no_voice_relevant_change profile={self.profile} mic={self.current_mic or '<unset>'}")
            return
        self.clear_absent()
        providers = _text(_path("JASPER_VOICE_PROVIDER_IDS_FILE", "/var/lib/jasper/voice_provider_ids")).splitlines()
        provider_file = read_env_file(_path("JASPER_VOICE_PROVIDER_FILE", "/var/lib/jasper/voice_provider.env"))
        provider = provider_file.get("JASPER_VOICE_PROVIDER", "")
        configured = (provider and provider in providers) or self.values.get("JASPER_VOICE_PROVIDER", "") in providers
        self.reset("jasper-voice.service")
        if not configured:
            self.log("event=aec_reconcile.voice_provider_unset action=none owner=jasper-voice.service")
            return
        self.system("--no-block", "restart", "jasper-voice.service")
        try:
            if not self.installed_build:
                _unlink(self.restart_stamp)
                return
            self.restart_stamp.parent.mkdir(parents=True, exist_ok=True)
            self.restart_stamp.write_text(self.voice_inputs() + "\n")
        except OSError:
            _unlink(self.restart_stamp)

    def bridge_want(self, enabled: bool) -> None:
        try:
            if enabled:
                self.want_dropin.parent.mkdir(parents=True, exist_ok=True)
                self.want_dropin.write_text("[Unit]\nWants=jasper-aec-bridge.service\n")
            elif self.want_dropin.exists():
                self.want_dropin.unlink()
            else:
                return
        except OSError:
            return
        self.system("daemon-reload")

    def outputd_restart(self, strict: bool = False) -> bool:
        if not self.outputd_restart_needed:
            return True
        self.reset("jasper-outputd.service")
        success = self.system("restart", "jasper-outputd.service")
        if success or not strict:
            self.outputd_restart_needed = False
        return success or not strict

    def write_legs(self, bridge: str, raw: bool = False, dtln: bool = False,
                   chip: bool = False, chip150: bool = False, chip210: bool = False) -> None:
        running = bridge == "1"
        reference = bridge == "reference"
        chip = (running and chip) or reference
        software = running and not chip
        port = self.values.get("JASPER_AEC_OUTPUTD_REF_UDP_PORT") or "9891"
        observed = software and self.observe_ref and bool(self.chip_ref_pcm)
        updates = {
            "JASPER_MIC_DEVICE_RAW": f"udp:{self.values.get('JASPER_AEC_UDP_PORT_RAW') or '9877'}" if software and raw else "",
            "JASPER_MIC_DEVICE_DTLN": f"udp:{self.values.get('JASPER_AEC_UDP_PORT_DTLN') or '9878'}" if software and dtln else "",
            "JASPER_AEC_DTLN_ENABLED": str(int(software and dtln)),
            "JASPER_MIC_DEVICE_CHIP_AEC_150": f"udp:{self.values.get('JASPER_AEC_UDP_PORT_CHIP_AEC_150') or '9887'}" if running and chip and chip150 else "",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": f"udp:{self.values.get('JASPER_AEC_UDP_PORT_CHIP_AEC_210') or '9888'}" if running and chip and chip210 else "",
            "JASPER_AEC_CHIP_AEC_ENABLED": str(int(chip)),
            "JASPER_AEC_REF_SOURCE": "outputd_udp",
            "JASPER_AEC_OUTPUTD_REF_UDP_HOST": "127.0.0.1",
            "JASPER_AEC_OUTPUTD_REF_UDP_PORT": port,
        }
        output = {
            "JASPER_OUTPUTD_CHIP_REF_PCM": self.chip_ref_pcm if chip or observed else "",
            "JASPER_OUTPUTD_REFERENCE_UDP_TARGET": f"127.0.0.1:{port}" if running else "",
            "JASPER_OUTPUTD_CHIP_REF_OBSERVE": str(int(observed)),
        }
        timing = {
            "JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE": str(xvf3800.CHIP_AEC_REFERENCE_SAMPLE_RATE_HZ),
            "JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES": str(xvf3800.CHIP_AEC_REFERENCE_PERIOD_FRAMES),
            "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES": str(xvf3800.CHIP_AEC_REFERENCE_BUFFER_FRAMES),
        }
        producer_active = bool(self.values.get("JASPER_OUTPUTD_CHIP_REF_PCM") or self.values.get("JASPER_OUTPUTD_REFERENCE_UDP_TARGET"))
        for key, value in (output | (timing if chip or producer_active else {})).items():
            if self.values.get(key, "") != value:
                self.outputd_restart_needed = True
        self.publish(updates | output | timing)
        if observed:
            self.log("chip-ref observe mode: arming chip-ref writer for drift measurement; mic path stays software AEC3")

    def stop_aec(self) -> None:
        if self.system("is-active", "--quiet", "jasper-aec-bridge.service"):
            self.voice_restart_needed = True
        self.ready(False)
        self.system("stop", "jasper-aec-bridge.service", "jasper-aec-init.service")
        self.system("disable", "jasper-aec-bridge.service", "jasper-aec-init.service")
        self.bridge_want(False)
        self.reset("jasper-aec-bridge.service", "jasper-aec-init.service")
        self.write_legs("0")
        self.outputd_restart()

    def start_aec(self) -> None:
        self.voice_restart_needed = True
        self.ready(True)
        self.system("enable", "jasper-aec-init.service", "jasper-aec-bridge.service")
        self.bridge_want(True)
        self.reset("jasper-aec-init.service", "jasper-aec-bridge.service")
        self.system("restart", "jasper-aec-init.service")
        self.system("restart", "jasper-aec-bridge.service")

    def park(self, code: str, reason: str) -> None:
        self.mark_absent(code, reason)
        self.ready(False)
        self.system("stop", "jasper-voice.service", "jasper-aec-bridge.service")
        self.reset("jasper-voice.service", "jasper-aec-bridge.service")
        self.log(f"managed XVF parked: {reason}")

    def alignment(self, disposition: str, **kwargs: str) -> None:
        self.commit_alignment(alignment_health(disposition, selection=self.profile, **kwargs))

    def commit_alignment(self, health: AlignmentHealth) -> None:
        health = replace(health, reason=" ".join(health.reason.split()).replace("'", "")[:400])
        self.publish(health.to_env())

    def init_alignment(self) -> bool:
        try:
            with self.alignment_path.open() as handle:
                # aec-init publishes shell quoting, so parse its four assignments
                # as data; never execute a record left by another process.
                lines = handle.read(4096).splitlines()
            values = {}
            for line in lines:
                key, sep, raw = line.partition("=")
                if sep and key in AlignmentHealth("").to_env():
                    tokens = shlex.split(raw)
                    values[key] = tokens[0] if tokens else ""
            health = AlignmentHealth.from_env(values)
            if not health.status:
                return False
        except (OSError, ValueError):
            return False
        self.commit_alignment(health)
        return True

    def fault(self, disposition: str, reason: str) -> None:
        _unlink(self.ready_marker)
        self.alignment(disposition)
        self.write_legs("reference")
        self.outputd_restart()
        self.park("chip_aec_bringup_failed", reason)

    def disclose(self) -> None:
        self.log(f"chip-AEC disclosed stale: {self.values.get('JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON', '')}")
        bridge_up = False
        if self.aec_ready():
            self.write_legs("1", raw=True)
            if self.aec_skip():
                self.ready(True)
                self.log(f"event=aec_reconcile.disclosed_bounce_skipped reason=no_voice_relevant_change alignment=disclosed_stale profile={self.profile}")
            else:
                self.start_aec()
            bridge_up = self.system("is-active", "--quiet", "jasper-aec-bridge.service")
        if bridge_up:
            self.outputd_restart()
            self.publish({"JASPER_MIC_DEVICE": self.udp_device})
        else:
            self.stop_aec()
            self.publish({"JASPER_MIC_DEVICE": self.aec_mic})
        self.restart_voice()

    def activate_chip(self) -> None:
        settled = self.values.get("JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS") == "ready" or (
            self.values.get("JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS") == "disclosed_stale"
            and self.values.get("JASPER_AEC_CHIP_AEC_ENABLED") == "1"
        )
        if settled and self.aec_skip():
            self.ready(True)
            self.log(f"event=aec_reconcile.chip_aec_bounce_skipped reason=no_voice_relevant_change alignment={self.values.get('JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS')} profile={self.profile} mic={self.current_mic}")
            return
        self.voice_restart_needed = True
        self.alignment("checking")
        self.mark_absent("chip_aec_validating", "validating commissioned chip-AEC alignment")
        self.system("stop", "jasper-voice.service", "jasper-aec-bridge.service")
        self.reset("jasper-voice.service", "jasper-aec-bridge.service")
        self.write_legs("1", chip=True)
        if not self.outputd_restart(strict=True):
            self.fault("reference_producer_down", "chip-reference producer failed; inspect jasper-outputd")
            return
        self.system("enable", "jasper-aec-init.service", "jasper-aec-bridge.service")
        self.bridge_want(True)
        self.reset("jasper-aec-init.service", "jasper-aec-bridge.service")
        _unlink(self.alignment_path)
        if self.system("restart", "jasper-aec-init.service"):
            if not self.init_alignment():
                self.alignment("applied")
            self.ready(True)
            if (not self.system("restart", "jasper-aec-bridge.service")
                    or not self.system("is-active", "--quiet", "jasper-aec-bridge.service")):
                self.fault("bridge_failed", "chip-AEC bridge failed; inspect jasper-aec-bridge")
                return
            self.restart_voice()
        elif self.init_alignment() and self.values.get("JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS") == "disclosed_stale":
            self.disclose()
        else:
            self.fault("reapply_failed", "chip-AEC alignment reapply failed; inspect jasper-aec-init")

    def voice_mic(self, spec: str) -> bool:
        return card_id(spec) in self.facts.channels and card_id(spec) not in self.facts.measurement_cards

    def aec_ready(self) -> bool:
        return self.voice_mic(self.aec_mic) and self.facts.channels[card_id(self.aec_mic)] == xvf3800.RECOMMENDED_CAPTURE_CHANNELS

    def owned_mic(self) -> bool:
        return (not self.current_mic or self.current_mic.startswith("udp:")
                or bool(re.fullmatch(r"hw:[0-9]+,1", self.current_mic))
                or self.current_mic in self.candidates)

    def repair_mixer(self) -> None:
        for control, value in (
            (xvf3800.MIXER_CAPTURE_SWITCH, "on"),
            (xvf3800.MIXER_CAPTURE_VOLUME, str(xvf3800.MIXER_VOLUME_MAX)),
        ):
            command = ["amixer", "-c", card_id(self.aec_mic), "cset", f"name={control}", ",".join([value] * xvf3800.RECOMMENDED_CAPTURE_CHANNELS)]
            self.mixer_command(command, control)
        self.mixer_command(["alsactl", "store"], "alsactl_store")

    def mixer_command(self, command: list[str], control: str) -> None:
        try:
            success = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0  # unbounded: retain manual ALSA wait; service/caller alone bounds the pass
        except OSError:
            success = False
        if not success:
            self.log(f"event=aec_reconcile.mixer_repair result=failed control={control}")

    def stop_voice(self) -> None:
        if self.facts.accessory_sources:
            self.log(f"event=aec_reconcile.accessory_mic status=present sources={','.join(self.facts.accessory_sources)}")
            self.accessory_restart_needed = True
            return
        code = "no_local_or_accessory_mic" if self.facts.accessory_status == "resolved" else "accessory_mic_unknown"
        detail = "no candidate microphone present and no accessory microphone paired" if code == "no_local_or_accessory_mic" else "no candidate microphone present; accessory microphone state could not be determined (probe failed)"
        self.mark_absent(code, detail)
        self.system("stop", "jasper-voice.service")
        self.reset("jasper-voice.service")

    def run(self) -> int:
        self.ready(False)
        if self.commission_active() and not self.arm:
            return 0
        if not self.arm:
            self.seed_mode()
            if self.reason == "install" or self.restart_intent.exists():
                self.voice_restart_needed = True
                intent = re.sub(r"[^a-zA-Z0-9_.-]", "", _text(self.restart_intent)) or self.reason
                _unlink(self.restart_intent)
                self.log(f"event=aec_reconcile.voice_restart_intent reason={intent}")
        self.values.update(read_env_file(self.mode_path))
        raw_profile = self.values.get("JASPER_AUDIO_INPUT_PROFILE", "custom")
        self.profile = normalize_audio_input_profile(raw_profile)
        self.values["JASPER_AUDIO_INPUT_PROFILE"] = self.profile
        self.mode = normalize_aec_mode(self.values.get("JASPER_AEC_MODE", "auto"))
        self.observe_ref = parse_env_bool(self.values.get("JASPER_AEC_CHIP_REF_OBSERVE", "0"))
        self.legs = [parse_env_bool(self.values.get(key) or str(int(default)), default) for _, key, default in WAKE_LEG_DEFAULTS]
        normalized = {"JASPER_AEC_MODE": self.mode,
                      "JASPER_AEC_CHIP_REF_OBSERVE": str(int(self.observe_ref))}
        normalized.update({key: str(int(leg)) for (_, key, _), leg in zip(WAKE_LEG_DEFAULTS, self.legs)})
        for key, value in normalized.items():
            raw = self.values.get(key, "")
            if raw and raw != value:
                self.log(f"event=audio_input_profile.normalized key={key} raw={shlex.quote(raw)} value={value}")
        self.facts = observe(self.values, _path("JASPER_ASOUND_ROOT", "/proc/asound"),
                             os.environ.get("JASPER_OUTPUTD_CONTROL_SOCKET", "/run/jasper-outputd/control.sock"), self.log)
        mic = self.facts.mic
        self.aec_mic = self.values.get("JASPER_AEC_MIC_DEVICE", "")
        default_candidates = ",".join((mic.alsa_card_name if mic else "", self.aec_mic, *xvf3800.ALSA_CARD_NAMES))
        self.candidates = list(dict.fromkeys((self.values.get("JASPER_MIC_DEVICE_CANDIDATES") or default_candidates).replace(",", " ").split()))
        if self.profile != "custom" and mic and card_id(mic.alsa_card_name) in self.facts.channels:
            old = self.aec_mic
            self.aec_mic = mic.alsa_card_name
            if self.aec_mic not in self.candidates:
                self.candidates.insert(0, self.aec_mic)
            if old != self.aec_mic:
                event = "rederived" if old else "derived"
                self.log(f"event=aec_reconcile.aec_mic_device_{event} profile={self.profile} old={old} new={self.aec_mic} variant={mic.variant_id} reason=detected_xvf_profile_wins")
        self.fallback_mic = next((candidate for candidate in self.candidates if card_id(candidate) not in self.facts.measurement_cards), xvf3800.ALSA_CARD_NAMES[0])
        self.present_mic = next((candidate for candidate in self.candidates if self.voice_mic(candidate)), "")
        self.aec_mic = self.aec_mic or self.present_mic or self.fallback_mic
        self.chip_ref_pcm = f"{xvf3800.CHIP_AEC_REFERENCE_PCM_ACCESS}:CARD={self.aec_mic},DEV={xvf3800.CHIP_AEC_REFERENCE_DEVICE_INDEX}"
        if self.profile == "custom" or not mic or not mic.alsa_card_name:
            self.chip_ref_pcm = self.values.get("JASPER_OUTPUTD_CHIP_REF_PCM") or self.chip_ref_pcm
        self.current_mic = self.values.get("JASPER_MIC_DEVICE", "")
        self.udp_device = f"udp:{self.values.get('JASPER_AEC_UDP_PORT') or '9876'}"
        if self.arm:
            self.write_legs("reference")
            return 0 if self.outputd_restart(strict=True) else 1
        if mic:
            try:
                atomic_write_json(_path("JASPER_MIC_PROFILE_STATE_PATH", "/run/jasper-mic-profile/xvf3800.json"), mic.as_dict(), mode=0o644)
            except OSError as exc:
                self.log(f"event=aec_reconcile.mic_state status=failed error={exc}")
        self.publish_mic()
        self.publish({"JASPER_LOCAL_MIC_PRESENT": str(int(bool(self.present_mic))) if self.owned_mic() else "unknown"})
        if self.profile != "custom":
            self.publish({"JASPER_AEC_MIC_DEVICE": self.aec_mic})
        gate = self.facts.dac_gate
        self.publish({
            "JASPER_AEC_CHIP_AEC_TESTING_REQUESTED": str(int(self.profile == "xvf_chip_aec_testing")),
            "JASPER_AEC_CHIP_AEC_DAC_ID": gate.dac_id,
            "JASPER_AEC_CHIP_AEC_DAC_STATUS": gate.status,
            "JASPER_AEC_CHIP_AEC_DAC_SOURCE": gate.source,
            "JASPER_AEC_CHIP_AEC_DAC_DETAIL": gate.detail,
        })
        managed = self.profile != "custom" and (mic is None or mic.present)
        chip_available = bool(mic and mic.chip_aec_supported and mic.chip_beam_plan_id and gate.permits(testing_requested=False))
        if mic and not mic.present and self.profile.startswith("xvf_"):
            self.alignment("xvf_absent")
        if "JASPER_GROUPING_VOICE_PARK=1" in (self.group_text or "").splitlines():
            self.system("disable", "--now", "jasper-voice.service")
            self.reset("jasper-voice.service")
            self.stop_aec()
            return 0
        self.log(f"event=aec_reconcile.pass profile={self.profile} mode={self.mode} current_mic={self.current_mic or '<unset>'} aec_mic={self.aec_mic} candidates={' '.join(self.candidates)} legs={','.join(str(int(leg)) for leg in self.legs)}")
        if managed:
            if self.aec_ready():
                self.repair_mixer()
            if self.aec_ready() and chip_available:
                self.publish({"JASPER_MIC_DEVICE": self.udp_device})
                self.activate_chip()
            elif not self.voice_mic(self.aec_mic):
                reason = "no usable managed XVF capture device present"
                self.alignment("xvf_unusable", reason=reason)
                self.park("xvf_capture_absent", reason)
                self.stop_aec()
            else:
                if not self.aec_ready():
                    self.alignment("disclosed", reason=XVF_2CH_DISCLOSURE, action=XVF_2CH_ACTION)
                else:
                    self.alignment("dac_uncalibrated", reason="mic profile resolver failed" if mic is None else mic.reason if not mic.chip_aec_supported else gate.detail)
                self.disclose()
            return 0
        if self.profile != "custom":
            updates = resolve_profile_wake_legs(self.profile, chip_available=chip_available and (self.profile != "auto" or self.aec_ready()))
            self.mode = updates.get("JASPER_AEC_MODE", self.mode)
            self.legs = [updates.get(key, "0") == "1" for _, key, _ in WAKE_LEG_DEFAULTS]
        elif self.legs[2] and not (mic and mic.chip_aec_supported and mic.chip_beam_plan_id):
            self.legs[2:] = [False, False, False]
        if self.profile == "custom" and self.legs[2] and not gate.permits(testing_requested=False):
            self.log(f"custom chip-AEC leg requested on output_dac={gate.dac_id}; honouring the explicit leg: {gate.detail}")
        if not self.owned_mic():
            self.clear_absent()
            self.stop_aec()
            return 0
        if self.mode != "disabled" and self.aec_ready():
            self.repair_mixer()
            self.publish({"JASPER_MIC_DEVICE": self.udp_device})
            self.write_legs("1", *self.legs)
            self.outputd_restart()
            if self.aec_skip():
                self.ready(True)
                self.log(f"event=aec_reconcile.aec_stack_bounce_skipped reason=no_voice_relevant_change profile={self.profile}")
            else:
                self.start_aec()
            self.restart_voice()
            return 0
        if not self.present_mic:
            self.stop_voice()
        self.stop_aec()
        if self.present_mic:
            changed = self.current_mic != self.present_mic
            self.publish({"JASPER_MIC_DEVICE": self.present_mic})
            reason = "aec_disabled" if self.mode == "disabled" else "not_6_channel"
            self.log(f"event=aec_reconcile.direct_mic_selected reason={reason} mic={self.present_mic} changed={int(changed)}")
            self.restart_voice()
        else:
            stale = not self.current_mic or self.current_mic.startswith("udp:") or (self.mode != "disabled" and bool(re.fullmatch(r"hw:[0-9]+,1", self.current_mic)))
            if stale:
                self.publish({"JASPER_MIC_DEVICE": self.fallback_mic})
            self.log(f"event=aec_reconcile.no_candidate_mic current={self.current_mic} fallback={self.fallback_mic} cleared={int(stale)}")
            if self.accessory_restart_needed:
                self.restart_voice()
        return 0

    def publish_mic(self) -> None:
        mic = self.facts.mic
        self.publish({
            "JASPER_XVF_PRESENT": str(int(bool(mic and mic.present))),
            "JASPER_XVF_VARIANT": mic.variant_id if mic else "",
            "JASPER_XVF_DISPLAY_NAME": mic.display_name if mic else "",
            "JASPER_XVF_GEOMETRY": mic.geometry if mic else "",
            "JASPER_XVF_ALSA_CARD": mic.alsa_card_name if mic else "",
            "JASPER_XVF_CAPTURE_CHANNELS": str(mic.capture_channels) if mic and mic.capture_channels is not None else "",
            "JASPER_XVF_CHIP_BEAM_PLAN": mic.chip_beam_plan_id if mic else "",
            "JASPER_XVF_CHIP_AEC_SUPPORTED": str(int(bool(mic and mic.chip_aec_supported))),
            "JASPER_XVF_RECOMMENDED_PROFILE": mic.recommended_profile if mic else "",
            "JASPER_XVF_REASON": mic.reason if mic else "mic profile resolver failed",
            "JASPER_XVF_SUPPORTED_ALSA_CARDS": ",".join(xvf3800.ALSA_CARD_NAMES),
            "JASPER_XVF_RECOMMENDED_CHANNELS": str(xvf3800.RECOMMENDED_CAPTURE_CHANNELS),
        })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reason", default="manual")
    args = parser.parse_args(argv)
    try:
        return Reconcile(args.reason).run()
    except (OSError, ValueError) as exc:
        print(f"event=aec_reconcile.pass status=failed error={exc}", file=sys.stderr)
        return 1
