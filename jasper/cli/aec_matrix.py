"""XVF3800 chip-side AEC parameter test matrix — `jasper-aec-matrix`.

Runs the test plan defined in `docs/aec-chipside-final-test-plan.md`:
sweeps the chip's untested AEC and post-suppressor parameters
through 9 phases, measuring AEC contribution properly (EXTGAIN=-120
vs EXTGAIN=0 with everything else identical) using a 60-second
sustained log sweep at each cell.

Designed to be run once to conclusively decide whether chip-side
AEC can deliver useful attenuation in our external-DAC topology.

Usage:
    sudo /opt/jasper/.venv/bin/jasper-aec-matrix [--phase N] [--dry-run]

Output:
    /var/lib/jasper/aec-matrix-<timestamp>.json   — full results
    /var/lib/jasper/aec-matrix-<timestamp>.md     — comparison table

Safety:
    - Volume is read first, never raised above current.
    - All chip parameters are read pre-test and restored in finally.
    - jasper-voice is stopped during testing (frees XVF capture EP)
      and restarted on exit.
    - Never calls SAVE_CONFIGURATION (brick hazard).
    - Never calls TEST_AEC_DISABLE_CONTROL (no-recovery hazard).
    - Test signal is 5% FS log sweep at master_gain ≤ -32 dB → ~-50
      dBFS at the speaker. Below typical music level.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("jasper.aec_matrix")

# Sweep test signal: 60 sec log sweep 200-3400 Hz at 5% FS, stereo 48k
SWEEP_DURATION_SEC = 60
SWEEP_F0 = 200.0
SWEEP_F1 = 3400.0
SWEEP_AMPLITUDE_FS = 0.05
SWEEP_RATE = 48000

# How long to wait for AEC to stabilize after parameter changes,
# before starting the measurement sweep.
PARAM_SETTLE_SEC = 1.0

# For PP_NLAEC_MODE train modes: extra warmup time so non-linear
# model can begin adapting before measurement starts.
TRAIN_WARMUP_SEC = 30

# Capture target: chip's USB capture, ALSA channel 0 (conference)
# on the 6-ch firmware (chip outputs FL FR FC LFE RL RR; chnl 0 =
# FL = conference processed). 16k S16_LE 6ch.
MIC_DEVICE = "hw:CARD=Array,DEV=0"
MIC_RATE = 16000
MIC_CHANNELS = 6
MIC_CHANNEL_INDEX = 0  # conference

# Test volume cap. Read current first; use min(this, current).
DEFAULT_TEST_VOLUME_DB = -32.0

# AEC-OFF marker: write this EXTGAIN value to functionally kill
# the reference signal. -120 dB attenuation = effectively zero.
AEC_OFF_EXTGAIN = -120.0
AEC_ON_EXTGAIN = 0.0

# Filter coefficient read uses the chip's chunked-read protocol.
COEFFS_PER_READ = 15

RESULTS_DIR = Path("/var/lib/jasper")


# ---------------------------------------------------------------
# Result types
# ---------------------------------------------------------------

@dataclass
class FilterDump:
    length: int
    rms: float
    peak_magnitude: float
    peak_tap_index: int
    nonzero_count: int

    def is_converged(self) -> bool:
        # Rough heuristic: structured convergence has peak <1.0,
        # substantial RMS (>1e-4), and a non-trivial number of
        # non-zero taps (>10% of filter length).
        return (
            self.peak_magnitude < 1.0
            and self.rms > 1e-4
            and self.nonzero_count > self.length * 0.1
        )


@dataclass
class CellResult:
    phase: int
    label: str
    params: dict[str, Any]
    aec_off_rms: float = 0.0
    aec_on_rms: float = 0.0
    attenuation_db: float = 0.0
    filter_pre: FilterDump | None = None
    filter_post: FilterDump | None = None
    converged: bool = False
    aec_aecconverged_flag: int = 0  # chip's own flag
    duration_sec: float = 0.0
    notes: str = ""


# ---------------------------------------------------------------
# xvf_host wrapper
# ---------------------------------------------------------------

class ChipControl:
    """Thin wrapper around jasper.xvf.xvf_host for ergonomic
    read/write and clean error surfaces. Maintains a cache of
    original values for the restore step."""

    def __init__(self) -> None:
        from ..xvf import xvf_host
        self._mod = xvf_host
        self._dev = None
        self._original_values: dict[str, list[Any]] = {}

    def __enter__(self) -> "ChipControl":
        for attempt in range(10):
            self._dev = self._mod.find()
            if self._dev is not None:
                break
            time.sleep(0.5)
        if self._dev is None:
            raise RuntimeError("XVF3800 not found on USB")
        return self

    def __exit__(self, *_exc) -> None:
        if self._dev is not None:
            try:
                self._dev.dev.close()
            except Exception:
                pass

    def read(self, name: str) -> list[Any]:
        if name not in self._mod.PARAMETERS:
            raise KeyError(f"parameter {name} not in firmware")
        return list(self._dev.read(name))

    def write(self, name: str, values: list[Any]) -> None:
        if name not in self._mod.PARAMETERS:
            raise KeyError(f"parameter {name} not in firmware")
        # Cache the original value once per parameter
        if name not in self._original_values:
            try:
                self._original_values[name] = self.read(name)
            except Exception as e:
                logger.warning("could not snapshot %s before write: %s", name, e)
                self._original_values[name] = None  # sentinel: skip restore
        self._dev.write(name, values)

    def restore_all(self) -> None:
        for name, original in self._original_values.items():
            if original is None:
                continue
            try:
                self._dev.write(name, original)
                logger.debug("restored %s = %s", name, original)
            except Exception as e:
                logger.warning("could not restore %s: %s", name, e)

    def dump_filter(self, far_index: int = 0, mic_index: int = 0) -> FilterDump:
        """Read SPECIAL_CMD_AEC_FILTER_COEFFS for the (far, mic) pair
        and compute summary stats. Side effect: leaves the chip's
        special-command state machine in a consistent state via
        AEC_FILTER_CMD_ABORT at the end."""
        try:
            self._dev.write("SPECIAL_CMD_AEC_FAR_MIC_INDEX", [far_index, mic_index])
            length = self.read("SPECIAL_CMD_AEC_FILTER_LENGTH")[0]
            coeffs: list[float] = []
            for off in range(0, length, COEFFS_PER_READ):
                self._dev.write("SPECIAL_CMD_AEC_FILTER_COEFF_START_OFFSET", [off])
                chunk = self.read("SPECIAL_CMD_AEC_FILTER_COEFFS")
                coeffs.extend(chunk)
                if len(coeffs) >= length:
                    break
            coeffs = coeffs[:length]
        finally:
            try:
                self._dev.write("AEC_FILTER_CMD_ABORT", [1])
            except Exception:
                pass
        if not coeffs:
            return FilterDump(length=0, rms=0.0, peak_magnitude=0.0,
                              peak_tap_index=0, nonzero_count=0)
        arr = np.asarray(coeffs, dtype=np.float64)
        abs_arr = np.abs(arr)
        peak_idx = int(np.argmax(abs_arr))
        return FilterDump(
            length=length,
            rms=float(np.sqrt(np.mean(arr * arr))),
            peak_magnitude=float(abs_arr[peak_idx]),
            peak_tap_index=peak_idx,
            nonzero_count=int(np.sum(abs_arr > 1e-6)),
        )

    def aec_converged_flag(self) -> int:
        try:
            return int(self.read("AEC_AECCONVERGED")[0])
        except Exception:
            return -1


# ---------------------------------------------------------------
# Camilla volume + voice service helpers
# ---------------------------------------------------------------

def camilla_get_volume() -> float:
    from camilladsp import CamillaClient
    c = CamillaClient("localhost", 1234)
    c.connect()
    try:
        return float(c.volume.main_volume())
    finally:
        c.disconnect()


def camilla_set_volume(db: float) -> None:
    from camilladsp import CamillaClient
    c = CamillaClient("localhost", 1234)
    c.connect()
    try:
        c.volume.set_main_volume(db)
    finally:
        c.disconnect()


def stop_voice_if_running() -> bool:
    r = subprocess.run(
        ["systemctl", "is-active", "jasper-voice.service"],
        capture_output=True, text=True,
    )
    if r.stdout.strip() == "active":
        logger.info("stopping jasper-voice")
        subprocess.run(["systemctl", "stop", "jasper-voice.service"], check=False)
        time.sleep(0.5)
        return True
    return False


def start_voice() -> None:
    logger.info("starting jasper-voice")
    subprocess.run(["systemctl", "start", "jasper-voice.service"], check=False)


# ---------------------------------------------------------------
# Sweep generation + RMS measurement
# ---------------------------------------------------------------

def generate_sweep(path: Path, duration_sec: int = SWEEP_DURATION_SEC) -> None:
    n = int(duration_sec * SWEEP_RATE)
    t = np.linspace(0, duration_sec, n, endpoint=False)
    # Exponential log sweep
    phase = (
        2 * np.pi * SWEEP_F0 * duration_sec
        / np.log(SWEEP_F1 / SWEEP_F0)
        * (np.exp(t / duration_sec * np.log(SWEEP_F1 / SWEEP_F0)) - 1)
    )
    mono = (np.sin(phase) * int(SWEEP_AMPLITUDE_FS * 32767)).astype(np.int16)
    stereo = np.stack([mono, mono], axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SWEEP_RATE)
        w.writeframes(stereo.tobytes())


def play_and_capture(sweep_wav: Path, out_wav: Path,
                     capture_duration_sec: int) -> None:
    """Play the sweep via _audioout (the moOde-hijacked Loopback path)
    and concurrently capture from the XVF chip's mic. The sweep flows
    through camilla → dongle → speaker → mic, exactly the path AEC
    needs to cancel."""
    play_proc = subprocess.Popen(
        ["aplay", "-q", "-D", "_audioout", str(sweep_wav)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)  # let the playback ramp up
    rec_proc = subprocess.run(
        [
            "arecord", "-q",
            "-d", str(capture_duration_sec),
            "-f", "S16_LE",
            "-r", str(MIC_RATE),
            "-c", str(MIC_CHANNELS),
            "-D", MIC_DEVICE,
            str(out_wav),
        ],
        check=False,
    )
    # Stop the player whether or not it finished
    if play_proc.poll() is None:
        play_proc.terminate()
        try:
            play_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            play_proc.kill()


def measure_rms(wav_path: Path, channel_index: int = MIC_CHANNEL_INDEX) -> float:
    if not wav_path.exists() or wav_path.stat().st_size < 1024:
        return 0.0
    with wave.open(str(wav_path), "rb") as w:
        n = w.getnframes()
        ch = w.getnchannels()
        raw = w.readframes(n)
    arr = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        arr = arr.reshape(-1, ch)[:, channel_index]
    return float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))


# ---------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------

def run_cell(chip: ChipControl, cell: CellResult, sweep_wav: Path,
             tmp: Path, dry_run: bool = False,
             capture_seconds: int | None = None) -> CellResult:
    """Apply cell.params, dump filter pre, run AEC-OFF + AEC-ON
    measurements, dump filter post, fill in the rest of the cell.

    AEC-OFF measurement: temporarily set EXTGAIN to -120 dB to kill
    the reference signal, leaving every other parameter (incl. all
    of cell.params) intact. AEC-ON measurement: restore EXTGAIN to
    the cell-specified or default value (typically 0 dB)."""
    cap_sec = capture_seconds or SWEEP_DURATION_SEC
    started = time.monotonic()

    # Apply cell parameters
    extgain_for_aec_on = AEC_ON_EXTGAIN
    for name, value in cell.params.items():
        if name == "AEC_FAR_EXTGAIN":
            extgain_for_aec_on = float(value)
            continue  # we manage EXTGAIN below
        if dry_run:
            logger.info("[dry-run] would set %s = %s", name, value)
        else:
            chip.write(name, value if isinstance(value, list) else [value])

    if dry_run:
        cell.notes = "dry-run, no measurement"
        return cell

    time.sleep(PARAM_SETTLE_SEC)

    # Phase-3 train modes need extra warmup
    if cell.phase == 3 and cell.params.get("PP_NLAEC_MODE", [0])[0] in (1, 2):
        logger.info(
            "  warming up NL training mode for %ds before measurement",
            TRAIN_WARMUP_SEC,
        )
        # Set EXTGAIN to ON during warmup so the chip can adapt
        chip.write("AEC_FAR_EXTGAIN", [extgain_for_aec_on])
        # Play sweep continuously during warmup (but discard recording)
        warmup_wav = tmp / f"warmup-{cell.phase}-{cell.label}.wav"
        play_and_capture(sweep_wav, warmup_wav, TRAIN_WARMUP_SEC)
        warmup_wav.unlink(missing_ok=True)

    # Filter dump pre (with AEC ON state already applied)
    chip.write("AEC_FAR_EXTGAIN", [extgain_for_aec_on])
    time.sleep(0.5)
    cell.filter_pre = chip.dump_filter()

    # AEC-OFF measurement: kill the reference
    logger.info("  AEC-OFF measurement (EXTGAIN=%.0f dB)…", AEC_OFF_EXTGAIN)
    chip.write("AEC_FAR_EXTGAIN", [AEC_OFF_EXTGAIN])
    time.sleep(PARAM_SETTLE_SEC)
    aec_off_wav = tmp / f"off-{cell.phase}-{cell.label}.wav"
    play_and_capture(sweep_wav, aec_off_wav, cap_sec)
    cell.aec_off_rms = measure_rms(aec_off_wav)
    aec_off_wav.unlink(missing_ok=True)

    # AEC-ON measurement: restore reference
    logger.info("  AEC-ON measurement  (EXTGAIN=%.0f dB)…", extgain_for_aec_on)
    chip.write("AEC_FAR_EXTGAIN", [extgain_for_aec_on])
    time.sleep(PARAM_SETTLE_SEC)
    aec_on_wav = tmp / f"on-{cell.phase}-{cell.label}.wav"
    play_and_capture(sweep_wav, aec_on_wav, cap_sec)
    cell.aec_on_rms = measure_rms(aec_on_wav)
    aec_on_wav.unlink(missing_ok=True)

    if cell.aec_off_rms > 1.0:
        cell.attenuation_db = 20.0 * math.log10(
            max(cell.aec_on_rms, 1.0) / cell.aec_off_rms
        )
    else:
        cell.attenuation_db = 0.0

    # Filter dump post + chip's own convergence flag
    cell.filter_post = chip.dump_filter()
    cell.aec_aecconverged_flag = chip.aec_converged_flag()
    cell.converged = (
        cell.filter_post is not None
        and cell.filter_post.is_converged()
    )
    cell.duration_sec = time.monotonic() - started
    return cell


# ---------------------------------------------------------------
# Test plan: phases and cells
# ---------------------------------------------------------------

def build_plan() -> list[CellResult]:
    """Build the ordered test plan per docs/aec-chipside-final-test-plan.md.
    Each cell is a CellResult with empty results to be filled in."""
    plan: list[CellResult] = []

    # Phase 1: Long-soak baseline. AGC off, default everything else.
    # SYS_DELAY held at the existing tuned value (whatever's in
    # /var/lib/jasper/aec_delay.txt; default to 96 if absent).
    sys_delay = 96
    p = Path("/var/lib/jasper/aec_delay.txt")
    if p.exists():
        try:
            sys_delay = int(p.read_text().strip())
        except ValueError:
            pass

    plan.append(CellResult(
        phase=1, label="baseline_longsoak",
        params={
            "PP_AGCONOFF": [0],
            "AUDIO_MGR_SYS_DELAY": [sys_delay],
            "SHF_BYPASS": [0],
        },
    ))

    # Phase 2: PP_ECHOONOFF + PP_GAMMA_* combinations. Highest-priority.
    for echo, ge, ge_tail, ge_nl, label in [
        (0, 1.0, 1.0, 1.0, "echo_off"),
        (1, 1.0, 1.0, 1.0, "echo_on_default"),
        (1, 1.5, 1.5, 2.0, "echo_on_aggressive"),
        (1, 2.0, 2.0, 5.0, "echo_on_max"),
    ]:
        plan.append(CellResult(
            phase=2, label=label,
            params={
                "PP_AGCONOFF": [0],
                "AUDIO_MGR_SYS_DELAY": [sys_delay],
                "SHF_BYPASS": [0],
                "PP_ECHOONOFF": [echo],
                "PP_GAMMA_E": [ge],
                "PP_GAMMA_ETAIL": [ge_tail],
                "PP_GAMMA_ENL": [ge_nl],
            },
        ))

    # Phase 3: PP_NLATTENONOFF + PP_NLAEC_MODE training modes
    for nl_atten, nl_mode, label in [
        (0, 0, "nl_off"),
        (1, 0, "nl_on_normal"),
        (1, 1, "nl_on_train"),
        (1, 2, "nl_on_train2"),
    ]:
        plan.append(CellResult(
            phase=3, label=label,
            params={
                "PP_AGCONOFF": [0],
                "AUDIO_MGR_SYS_DELAY": [sys_delay],
                "SHF_BYPASS": [0],
                "PP_NLATTENONOFF": [nl_atten],
                "PP_NLAEC_MODE": [nl_mode],
            },
        ))

    # Phase 4: AEC_AECEMPHASISONOFF
    for emphasis, label in [
        (0, "emphasis_off"),
        (1, "emphasis_on"),
        (2, "emphasis_on_eq"),
    ]:
        plan.append(CellResult(
            phase=4, label=label,
            params={
                "PP_AGCONOFF": [0],
                "AUDIO_MGR_SYS_DELAY": [sys_delay],
                "SHF_BYPASS": [0],
                "AEC_AECEMPHASISONOFF": [emphasis],
            },
        ))

    # Phase 5: Disable PCD by setting AEC_PCD_COUPLINGI out of range
    plan.append(CellResult(
        phase=5, label="pcd_disabled",
        params={
            "PP_AGCONOFF": [0],
            "AUDIO_MGR_SYS_DELAY": [sys_delay],
            "SHF_BYPASS": [0],
            "AEC_PCD_COUPLINGI": [-1.0],  # out of [0,1] range = disabled
        },
    ))

    # Phase 6: AUDIO_MGR_REF_GAIN sweep
    for ref_gain, label in [
        (-20.0, "ref_gain_-20"),
        (0.0, "ref_gain_0"),  # baseline; default
        (6.0, "ref_gain_+6"),
        (12.0, "ref_gain_+12"),
        (20.0, "ref_gain_+20"),
    ]:
        plan.append(CellResult(
            phase=6, label=label,
            params={
                "PP_AGCONOFF": [0],
                "AUDIO_MGR_SYS_DELAY": [sys_delay],
                "SHF_BYPASS": [0],
                "AUDIO_MGR_REF_GAIN": [ref_gain],
            },
        ))

    # Phase 7: AEC_ASROUTONOFF — switch between ASR-processed (1) and
    # AEC residuals per mic (0). Note: when 0, the chip's USB capture
    # changes its content — channel 0 won't be "conference" anymore.
    # We still measure channel 0 RMS for consistency.
    for asr, label in [
        (1, "asrout_on"),  # default: conference channel
        (0, "asrout_off"),  # AEC residuals
    ]:
        plan.append(CellResult(
            phase=7, label=label,
            params={
                "PP_AGCONOFF": [0],
                "AUDIO_MGR_SYS_DELAY": [sys_delay],
                "SHF_BYPASS": [0],
                "AEC_ASROUTONOFF": [asr],
            },
        ))

    # Phase 8: One-shot tests
    plan.append(CellResult(
        phase=8, label="far_end_dsp_enable",
        params={
            "PP_AGCONOFF": [0],
            "AUDIO_MGR_SYS_DELAY": [sys_delay],
            "SHF_BYPASS": [0],
            "AUDIO_MGR_FAR_END_DSP_ENABLE": [1],
        },
    ))

    # Phase 9: Best-of-best — fills in dynamically once we know the
    # winning combination. Implementation: skip during normal run;
    # caller can re-run with --phase 9 after analyzing previous
    # results and editing this function. (Auto-combine logic is
    # tricky and high-risk; a manual review step is safer.)

    return plan


# ---------------------------------------------------------------
# Output / reporting
# ---------------------------------------------------------------

def write_results(results: list[CellResult], baseline: dict[str, Any],
                  json_path: Path, md_path: Path) -> None:
    payload = {
        "baseline": baseline,
        "cells": [_serialize_cell(c) for c in results],
    }
    json_path.write_text(json.dumps(payload, indent=2))

    lines = ["# AEC chip-side test matrix — results", ""]
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append(f"Baseline master_gain: {baseline.get('master_gain_db', '?'):.1f} dB")
    lines.append(f"Test volume: {baseline.get('test_volume_db', '?'):.1f} dB")
    lines.append("")
    lines.append("## Per-cell results")
    lines.append("")
    lines.append("| Phase | Label | OFF rms | ON rms | Atten (dB) | Filter peak | Filter RMS | Conv? | Chip flag |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|---:|")
    for c in results:
        peak = f"{c.filter_post.peak_magnitude:.4f}" if c.filter_post else "-"
        f_rms = f"{c.filter_post.rms:.4f}" if c.filter_post else "-"
        lines.append(
            f"| {c.phase} | {c.label} | {c.aec_off_rms:.0f} | "
            f"{c.aec_on_rms:.0f} | {c.attenuation_db:+.1f} | {peak} | "
            f"{f_rms} | {'yes' if c.converged else 'no'} | "
            f"{c.aec_aecconverged_flag} |"
        )
    lines.append("")

    # Best result summary
    best = min(results, key=lambda c: c.attenuation_db) if results else None
    if best is not None:
        lines.append(f"## Best attenuation: phase {best.phase}, "
                     f"cell {best.label}: **{best.attenuation_db:+.1f} dB**")
        lines.append("")
        lines.append("Decision per docs/aec-chipside-final-test-plan.md:")
        if best.attenuation_db <= -15:
            lines.append("- ≥ -15 dB → adopt this combination as default; "
                         "tear down software AEC.")
        elif best.attenuation_db <= -5:
            lines.append("- -5 to -15 dB → compare against software AEC's "
                         "measured -8 dB peak. Likely chip-side wins on RAM "
                         "+ stability.")
        else:
            lines.append("- < -5 dB → chip-side conclusively dead in this "
                         "topology. Keep software AEC opt-in or accept "
                         "no-AEC default.")
    md_path.write_text("\n".join(lines) + "\n")


def _serialize_cell(c: CellResult) -> dict[str, Any]:
    d = asdict(c)
    return d


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="XVF3800 chip-side AEC parameter sweep test matrix"
    )
    parser.add_argument("--phase", type=int, default=None,
                        help="Run only the given phase (1-9); default: all phases 1-8")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen, don't actually test")
    parser.add_argument("--test-volume-db", type=float,
                        default=DEFAULT_TEST_VOLUME_DB,
                        help=f"Target test volume (default: {DEFAULT_TEST_VOLUME_DB} "
                             "dB). Will be clamped to min(target, current).")
    parser.add_argument("--capture-seconds", type=int, default=None,
                        help="Override capture duration per cell (for fast iteration)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s aec-matrix %(levelname)s %(message)s",
    )

    if os.geteuid() != 0:
        logger.error("must run as root (chip USB control + systemctl)")
        return 1

    plan = build_plan()
    if args.phase is not None:
        plan = [c for c in plan if c.phase == args.phase]
        if not plan:
            logger.error("no cells in phase %d", args.phase)
            return 1
    logger.info("test plan: %d cells across %d phases",
                len(plan), len({c.phase for c in plan}))

    # Volume safety
    original_volume = camilla_get_volume()
    test_volume = min(args.test_volume_db, original_volume)
    if test_volume >= original_volume:
        logger.info(
            "current master_gain (%.1f) is already at-or-below target (%.1f); "
            "no ducking needed", original_volume, args.test_volume_db,
        )
    else:
        logger.info(
            "ducking master_gain: %.1f → %.1f dB during test",
            original_volume, test_volume,
        )

    voice_was_active = stop_voice_if_running()

    sweep_dir = tempfile.mkdtemp(prefix="aec-matrix-")
    sweep_path = Path(sweep_dir)
    sweep_wav = sweep_path / "sweep.wav"
    cap_seconds = args.capture_seconds or SWEEP_DURATION_SEC
    generate_sweep(sweep_wav, duration_sec=max(cap_seconds + 5, SWEEP_DURATION_SEC))
    logger.info("generated sweep: %s (%d sec)", sweep_wav, cap_seconds)

    baseline: dict[str, Any] = {
        "master_gain_db": original_volume,
        "test_volume_db": test_volume,
        "capture_seconds": cap_seconds,
        "voice_was_active": voice_was_active,
    }

    results: list[CellResult] = []

    # Apply test volume
    if not args.dry_run and test_volume < original_volume:
        camilla_set_volume(test_volume)

    try:
        with ChipControl() as chip:
            # Capture chip's pre-test parameter snapshot
            try:
                version = chip.read("VERSION")
                baseline["chip_version"] = ".".join(str(v) for v in version)
                logger.info("XVF firmware: %s", baseline["chip_version"])
            except Exception as e:
                logger.warning("could not read chip version: %s", e)

            for i, cell in enumerate(plan, start=1):
                logger.info("[%d/%d] phase %d: %s",
                            i, len(plan), cell.phase, cell.label)
                logger.info("  params: %s", cell.params)
                try:
                    run_cell(chip, cell, sweep_wav, sweep_path,
                             dry_run=args.dry_run, capture_seconds=cap_seconds)
                    if not args.dry_run:
                        logger.info(
                            "  → off=%.0f on=%.0f attn=%+.1f dB peak=%s converged=%s",
                            cell.aec_off_rms, cell.aec_on_rms,
                            cell.attenuation_db,
                            f"{cell.filter_post.peak_magnitude:.4f}"
                            if cell.filter_post else "-",
                            cell.converged,
                        )
                except KeyError as e:
                    cell.notes = f"parameter not in firmware: {e}"
                    logger.warning("  skipped: %s", cell.notes)
                except Exception as e:
                    cell.notes = f"error: {e}"
                    logger.exception("  cell failed")
                results.append(cell)
            logger.info("restoring chip parameters…")
            chip.restore_all()
    finally:
        if not args.dry_run:
            try:
                camilla_set_volume(original_volume)
                logger.info("restored master_gain to %.1f dB", original_volume)
            except Exception:
                logger.warning("could not restore master_gain")
        if voice_was_active:
            start_voice()
        # Don't delete sweep_dir on dry-run — might be useful
        if not args.dry_run:
            try:
                for f in sweep_path.iterdir():
                    f.unlink()
                sweep_path.rmdir()
            except Exception:
                pass

    # Write results
    if not args.dry_run:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        json_path = RESULTS_DIR / f"aec-matrix-{ts}.json"
        md_path = RESULTS_DIR / f"aec-matrix-{ts}.md"
        write_results(results, baseline, json_path, md_path)
        logger.info("results: %s", md_path)
        logger.info("        : %s", json_path)
        # Also print the summary table to stdout
        print()
        print(md_path.read_text())

    return 0


if __name__ == "__main__":
    sys.exit(main())
