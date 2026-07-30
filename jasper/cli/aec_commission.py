# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Explicit foreground commissioning for managed XVF3800 chip AEC."""

from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

import numpy as np

from jasper.atomic_io import atomic_write_json
from jasper.audio_hardware import dac as dac_registry
from jasper.chip_aec_alignment import (
    ARTIFACT_PATH,
    AlignmentArtifact,
    MAX_CENTER_ERROR,
    MIN_EDGE_MARGIN,
    MicTiming,
    ProductResult,
    analyze_product,
    analyze_timing,
    choose_delay,
    commissioning_stimulus,
    median_samples,
    read_capture,
    WINDOW_CENTER,
)
from jasper.cli import aec_init
from jasper.cli.aec_tune import _camilla_get_volume, _camilla_set_volume
from jasper.env_load import merged_env_files
from jasper.log_event import log_event
from jasper.mics import xvf3800
from jasper.route_latency.status_socket import read_status_socket

logger = logging.getLogger("jasper.aec_commission")
MARKER = Path("/run/jasper-chip-aec-commission/active")
MEASUREMENT_VOLUME_DB = -28.282827
INITIAL_SYS_DELAY = xvf3800.CHIP_AEC_SYS_DELAY_DEFAULT
ADAPTATION_REPEATS = 90
STOP_UNITS = (
    "jasper-voice.service",
    "jasper-aec-bridge.service",
    "jasper-aec-init.service",
    "jasper-outputd.service",
)


class CommissioningError(RuntimeError):
    pass


@dataclass(frozen=True)
class Hardware:
    variant: str
    card: str
    plan: xvf3800.ChipBeamPlan


@dataclass(frozen=True)
class QueueWindow:
    status: Mapping[str, Any]
    samples: tuple[int, ...]


def _run(command: Sequence[str], *, timeout: float = 30) -> None:
    result = subprocess.run(
        list(command), capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise CommissioningError(f"{' '.join(command)}: {detail}")


def _create_marker(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise CommissioningError(f"commissioning is already active: {path}") from exc
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _final_timing(evidence: Sequence[MicTiming]) -> tuple[tuple[int, ...], float, int]:
    lags = tuple(item.lag for item in evidence)
    center = (min(lags) + max(lags)) / 2
    margin = min(
        min(
            item.lag - item.uncertainty - xvf3800.CHIP_AEC_FIRST_PEAK_HARD_MIN,
            xvf3800.CHIP_AEC_FIRST_PEAK_HARD_MAX - item.lag - item.uncertainty,
        )
        for item in evidence
    )
    if margin < MIN_EDGE_MARGIN:
        raise CommissioningError(f"final causal-window margin is only {margin} samples")
    if abs(center - WINDOW_CENTER) > MAX_CENTER_ERROR:
        raise CommissioningError(f"final causal-window center is {center:.1f} samples")
    return lags, center, margin


def _writer_counters(window: QueueWindow) -> tuple[int, ...]:
    refs = window.status["reference_outputs"]
    writer = refs["chip_ref_writer"]
    return tuple(
        int(writer[name]) for name in aec_init.REFERENCE_WRITER_COUNTER_NAMES
    )


def _commission(io, artifact_path: Path) -> tuple[AlignmentArtifact, dict[str, Any]]:
    hardware = io.detect()
    stereo, reference, active = commissioning_stimulus()
    original_volume = io.prepare_volume()
    dev = None
    try:
        io.stop_services()
        dev = io.reset_once(hardware)
        io.start_outputd()
        initial_queue = io.queue(hardware)
        identity = io.identity(dev, hardware, initial_queue.status)

        with tempfile.TemporaryDirectory(prefix="jasper-chip-aec-") as raw:
            directory = Path(raw)
            stimulus = directory / "commissioning.wav"
            with wave.open(str(stimulus), "wb") as wav:
                wav.setparams((2, 2, 48_000, 0, "NONE", ""))
                wav.writeframes(stereo.tobytes())

            io.apply(dev, hardware, INITIAL_SYS_DELAY, arm=False)
            if io.convergence(dev) != 0:
                raise CommissioningError("volatile reset did not clear convergence")
            io.warmup(hardware, stimulus, directory)
            initial = tuple(
                MicTiming(
                    mic,
                    INITIAL_SYS_DELAY,
                    io.timing(dev, hardware, mic, stimulus, reference, directory, "initial"),
                )
                for mic in range(4)
            )
            choice = choose_delay(initial)
            log_event(
                logger,
                "chip_aec_commission.timing_selected",
                sys_delay=choice.sys_delay,
                projected_lags=choice.projected_lags,
                worst_edge_margin=choice.worst_edge_margin,
            )

            before = io.queue(hardware)
            io.apply(dev, hardware, choice.sys_delay, arm=False)
            final = tuple(
                MicTiming(
                    mic,
                    choice.sys_delay,
                    io.timing(dev, hardware, mic, stimulus, reference, directory, "final"),
                )
                for mic in range(4)
            )
            after = io.queue(hardware)
            if len({_writer_counters(window) for window in (initial_queue, before, after)}) != 1:
                raise CommissioningError("writer errors changed during commissioning")
            before_median = median_samples(before.samples)
            queue_median = median_samples(after.samples)
            if abs(queue_median - before_median) > 2:
                raise CommissioningError("reference queue moved during final timing")
            lags, center, margin = _final_timing(final)

            io.apply(dev, hardware, choice.sys_delay, arm=True)
            io.adapt(stimulus, directory)
            if io.convergence(dev) != 1:
                raise CommissioningError("AEC convergence did not transition 0 to 1")
            product = io.product(
                dev, hardware, choice.sys_delay, stimulus, active, directory
            )
            artifact = AlignmentArtifact(identity, choice.sys_delay + queue_median)
            evidence: dict[str, Any] = {
                "sys_delay": choice.sys_delay,
                "k_samples": artifact.k_samples,
                "queue_median": queue_median,
                "queue_spread": max(after.samples) - min(after.samples),
                "lags": lags,
                "center": center,
                "worst_edge_margin": margin,
                "raw_excess_snr_db": round(product.minimum_raw_excess_snr_db, 2),
                "raw_level_delta_db": round(product.raw_level_delta_db, 3),
                "beam_acquisition_db": tuple(
                    round(value, 2) for value in product.beam_acquisition_db
                ),
                "beam_suppression_db": tuple(
                    round(value, 2) for value in product.beam_suppression_db
                ),
                "clipped_samples": product.clipped_samples,
            }
    finally:
        try:
            if dev is not None:
                io.close(dev)
        finally:
            io.restore_volume(original_volume)

    # Publication is the commit point, after device and household-volume cleanup.
    atomic_write_json(
        artifact_path,
        artifact.to_dict(),
        mode=0o640,
        group_from_parent=True,
        durable=True,
    )
    return artifact, evidence


def run_commissioning(
    io=None,
    *,
    marker_path: Path = MARKER,
    artifact_path: Path = ARTIFACT_PATH,
    effective_uid: int | None = None,
) -> AlignmentArtifact:
    if (os.geteuid() if effective_uid is None else effective_uid) != 0:
        raise CommissioningError("run jasper-aec-commission as root")
    runtime = SystemIO() if io is None else io
    _create_marker(marker_path)
    primary: BaseException | None = None
    try:
        runtime.wait_reconciler_idle()
        artifact, evidence = _commission(runtime, artifact_path)
        log_event(logger, "chip_aec_commission.passed", **evidence)
        print(
            "XVF chip-AEC commissioning passed: "
            f"SYS_DELAY={evidence['sys_delay']}, K={artifact.k_samples}, "
            f"lags={evidence['lags']}, suppression={evidence['beam_suppression_db']} dB"
        )
        return artifact
    except BaseException as exc:  # noqa: BLE001
        primary = exc
        log_event(
            logger, "chip_aec_commission.failed", reason=str(exc), level=logging.ERROR
        )
        raise
    finally:
        marker_path.unlink(missing_ok=True)
        try:
            runtime.reconcile()
        except BaseException as exc:  # noqa: BLE001
            if primary is not None:
                primary.add_note(f"reconcile cleanup also failed: {exc}")
            else:
                raise


class SystemIO:
    """Host-I/O adapter; orchestration above stays focused and testable."""

    def __init__(self) -> None:
        self.env: Mapping[str, str] = {}

    def wait_reconciler_idle(self) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["systemctl", "is-active", "jasper-aec-reconcile.service"],
                capture_output=True,
                text=True,
            )
            if result.stdout.strip() in {"inactive", "failed", "unknown"}:
                self.env = merged_env_files()
                return
            time.sleep(0.2)
        raise CommissioningError("AEC reconciler did not become idle")

    def assert_audio_idle(self) -> None:
        try:
            mux = read_status_socket("/run/jasper-mux/control.sock")
            fanin = read_status_socket("/run/jasper-fanin/control.sock")
            sources = mux.get("sources")
            inputs = fanin.get("inputs")
            tts = fanin.get("tts")
            if (
                mux.get("active_source") != "idle"
                or mux.get("selected_source") is not None
                or mux.get("winner") is not None
                or not isinstance(sources, Mapping)
                or any(
                    isinstance(source, Mapping) and source.get("playing") is True
                    for source in sources.values()
                )
                or not isinstance(inputs, list)
                or not isinstance(tts, Mapping)
                or fanin.get("selected_input") is not None
                or fanin.get("selection_mode") != "none"
                or tts.get("pending_frames") != 0
                or tts.get("program_duck_active") is True
                or any(
                    isinstance(item, Mapping)
                    and float(item.get("rms_dbfs", 0.0)) > -80.0
                    for item in inputs
                )
            ):
                raise CommissioningError(
                    "ordinary audio is playing; stop it before commissioning"
                )
        except (OSError, TypeError, ValueError) as exc:
            raise CommissioningError(f"cannot verify idle audio path: {exc}") from exc

    def detect(self) -> Hardware:
        profile = xvf3800.detect_runtime_profile()
        dac = dac_registry.by_id(self.env.get("JASPER_AUDIO_DAC_ID", ""))
        if (
            not profile.present
            or profile.capture_channels != 6
            or not profile.chip_aec_supported
            or profile.chip_beam_plan is None
        ):
            raise CommissioningError(f"unsupported XVF: {profile.reason}")
        if dac is None or dac.chip_aec_qualification != "approved":
            raise CommissioningError("output profile is not approved for chip AEC")
        return Hardware(profile.variant_id, profile.alsa_card_name, profile.chip_beam_plan)

    def prepare_volume(self) -> float:
        self.assert_audio_idle()
        original = _camilla_get_volume()
        try:
            _camilla_set_volume(MEASUREMENT_VOLUME_DB)
            if not math.isclose(
                _camilla_get_volume(), MEASUREMENT_VOLUME_DB, abs_tol=0.05
            ):
                raise CommissioningError("commissioning volume did not verify")
        except BaseException:  # noqa: BLE001 - restore household volume on interrupt
            _camilla_set_volume(original)
            raise
        return original

    def restore_volume(self, original: float) -> None:
        if not math.isclose(_camilla_get_volume(), original, abs_tol=0.05):
            _camilla_set_volume(original)

    def stop_services(self) -> None:
        _run(("systemctl", "stop", *STOP_UNITS))

    def reset_once(self, hardware: Hardware):
        from jasper.xvf import xvf_host

        old = xvf_host.find()
        if old is None:
            raise CommissioningError("XVF control interface is unavailable")
        transfer_error = None
        try:
            old_serial = aec_init.xvf_factory_serial(old)
            try:
                old.reboot_once()
            except Exception as exc:  # noqa: BLE001  # disappearance can race USB
                transfer_error = exc
        finally:
            old.close()
        card_path = Path("/proc/asound") / hardware.card
        deadline = time.monotonic() + 10
        while card_path.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if card_path.exists():
            raise CommissioningError(f"XVF did not disappear after reset: {transfer_error}")
        deadline = time.monotonic() + 30
        dev = None
        while time.monotonic() < deadline:
            if card_path.exists():
                dev = xvf_host.find()
                if dev is not None:
                    break
            time.sleep(0.1)
        if dev is None:
            raise CommissioningError("XVF did not re-enumerate after reset")
        try:
            if aec_init.xvf_factory_serial(dev) != old_serial:
                raise CommissioningError("XVF physical identity changed across reset")
            current = self.detect()
            if (current.variant, current.card, current.plan.plan_id) != (
                hardware.variant, hardware.card, hardware.plan.plan_id
            ):
                raise CommissioningError("XVF identity changed across reset")
            aec_init.write_required(dev, "SHF_BYPASS", [1])
            aec_init.set_uac_unity(hardware.card)
        except BaseException:  # noqa: BLE001 - never leak a rejected USB handle
            dev.close()
            raise
        return dev

    def start_outputd(self) -> None:
        _run(("systemctl", "start", "jasper-outputd.service"))

    def queue(self, hardware: Hardware) -> QueueWindow:
        status, samples = aec_init.collect_reference_queue(
            aec_init.native_reference_pcm(hardware.card)
        )
        return QueueWindow(status, samples)

    def identity(self, dev, hardware: Hardware, status: Mapping[str, Any]):
        env = dict(self.env)
        env["JASPER_XVF_VARIANT"] = hardware.variant
        return aec_init.build_identity(dev, hardware.plan, status, env=env)

    def apply(self, dev, hardware: Hardware, delay: int, *, arm: bool) -> None:
        aec_init.apply_profile(dev, hardware.plan, delay, card=hardware.card, arm=arm)

    def convergence(self, dev) -> int:
        value = dev.read("AEC_AECCONVERGED")
        if not isinstance(value, Sequence) or len(value) != 1:
            raise CommissioningError("AEC convergence readback is invalid")
        return int(value[0])

    def _capture(self, hardware: Hardware, stimulus: Path, output: Path) -> np.ndarray:
        recorder = subprocess.Popen(
            [
                "arecord", "-q", "-D", f"hw:CARD={hardware.card},DEV=0",
                "-d", "2", "-f", "S16_LE", "-r", "16000", "-c", "6", str(output),
            ]
        )
        try:
            time.sleep(0.25)
            _run(("aplay", "-q", "-D", "correction_substream", str(stimulus)), timeout=5)
            if recorder.wait(timeout=5):
                raise CommissioningError("XVF capture failed")
        finally:
            if recorder.poll() is None:
                recorder.terminate()
                recorder.wait(timeout=2)
        return read_capture(output)

    def timing(
        self, dev, hardware: Hardware, mic: int, stimulus: Path,
        reference: np.ndarray, directory: Path, label: str,
    ) -> tuple[int, ...]:
        aec_init.write_required(dev, "AUDIO_MGR_OP_L", [3, mic])
        aec_init.write_required(dev, "AUDIO_MGR_OP_R", [12, 0])
        trials = []
        for trial in range(3):
            capture = self._capture(
                hardware, stimulus, directory / f"{label}-m{mic}-{trial}.wav"
            )
            result = analyze_timing(capture, reference)
            trials.append(result.lag)
            log_event(
                logger, "chip_aec_commission.timing_trial",
                phase=label, mic=mic, trial=trial + 1, lag=result.lag,
                peak=round(result.peak, 4), peak_ratio=round(result.peak_ratio, 4),
                mic_rms_dbfs=round(result.mic_rms_dbfs, 2),
                reference_rms_dbfs=round(result.reference_rms_dbfs, 2),
                clipped_samples=result.clipped_samples,
            )
        return tuple(trials)

    def warmup(self, hardware: Hardware, stimulus: Path, directory: Path) -> None:
        self._capture(hardware, stimulus, directory / "discarded-warmup.wav")

    def adapt(self, stimulus: Path, directory: Path) -> None:
        repeated = directory / "adaptation.wav"
        with wave.open(str(stimulus), "rb") as source:
            params, frames = source.getparams(), source.readframes(source.getnframes())
        with wave.open(str(repeated), "wb") as target:
            target.setparams(params)
            for _ in range(ADAPTATION_REPEATS):
                target.writeframesraw(frames)
        _run(("aplay", "-q", "-D", "correction_substream", str(repeated)), timeout=120)

    def product(
        self, dev, hardware: Hardware, delay: int, stimulus: Path,
        active: np.ndarray, directory: Path,
    ) -> ProductResult:
        on = self._capture(hardware, stimulus, directory / "aec-on.wav")
        aec_init.write_required(dev, "SHF_BYPASS", [1])
        try:
            off = self._capture(hardware, stimulus, directory / "aec-off.wav")
        finally:
            self.apply(dev, hardware, delay, arm=True)
        return analyze_product(on, off, active)

    def close(self, dev) -> None:
        dev.close()

    def reconcile(self) -> None:
        _run(("/usr/local/sbin/jasper-aec-reconcile", "--reason", "chip-aec-commission"))


def _signal(signum: int, _frame: FrameType | None) -> None:
    raise CommissioningError(f"received signal {signum}")


def main(argv: Sequence[str] | None = None) -> int:
    argparse.ArgumentParser(prog="jasper-aec-commission").parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s aec-commission %(levelname)s %(message)s"
    )
    handlers = {
        signum: signal.signal(signum, _signal) for signum in (signal.SIGTERM, signal.SIGHUP)
    }
    try:
        run_commissioning()
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        print(f"jasper-aec-commission: {exc}", file=sys.stderr)
        return 1
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
