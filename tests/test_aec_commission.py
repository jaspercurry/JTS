# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from jasper.chip_aec_alignment import (
    AlignmentIdentity,
    MicTiming,
    ProductResult,
    artifact_from_dict,
)
from jasper.cli import aec_commission
from jasper.mics import xvf3800


def _status(counter: int = 0) -> dict:
    return {
        "reference_outputs": {
            "chip_ref_writer": {
                "open_error_count": counter,
                "retry_count": 0,
                "write_underrun_count": 0,
                "write_xrun_count": 0,
                "write_error_count": 0,
                "write_recovery_count": 0,
                "dropped_periods_due_to_full_queue": 0,
                "dropped_periods_due_to_disconnected_writer": 0,
                "dropped_periods_while_unavailable": 0,
            }
        }
    }


class _FakeIO:
    def __init__(self, *, fail_product: bool = False) -> None:
        plan = xvf3800.SQUARE_FIXED_150_210_PLAN
        self.hardware = aec_commission.Hardware(
            "xvf3800_legacy_square_6ch", "Array", plan
        )
        self.live_identity = AlignmentIdentity(
            self.hardware.variant,
            "XVF3800-001",
            "a1f70651",
            plan.plan_id,
            xvf3800.chip_aec_fixed_profile_fingerprint(plan),
            "apple_usb_c_dongle",
            "usb-serial:DWH53530FLL2FN3A3",
            "single:outputd_dac",
            48_000,
            2,
            128,
            384,
        )
        self.events: list[str] = []
        self.queue_calls = 0
        self.convergence_values = iter((0, 1))
        self.fail_product = fail_product

    def wait_reconciler_idle(self) -> None:
        self.events.append("idle")

    def detect(self):
        return self.hardware

    def prepare_volume(self) -> float:
        self.events.append("volume_set")
        return -20.0

    def restore_volume(self, _original: float) -> None:
        self.events.append("volume_restored")

    def stop_services(self) -> None:
        self.events.append("services_stopped")

    def reset_once(self, _hardware):
        self.events.append("reset_once")
        return object()

    def start_outputd(self) -> None:
        self.events.append("outputd_started")

    def queue(self, _hardware):
        self.queue_calls += 1
        samples = (282,) * 8 if self.queue_calls == 1 else (283,) * 8
        return aec_commission.QueueWindow(_status(), samples)

    def identity(self, _dev, _hardware, _status):
        return self.live_identity

    def apply(self, _dev, _hardware, delay: int, *, arm: bool) -> None:
        self.events.append(f"apply:{delay}:{int(arm)}")

    def convergence(self, _dev) -> int:
        return next(self.convergence_values)

    def warmup(self, _hardware, _stimulus, _directory) -> None:
        self.events.append("discarded_warmup")

    def timing(
        self, _dev, _hardware, mic: int, _stimulus, _reference, _directory, label
    ) -> tuple[int, ...]:
        self.events.append(f"timing:{label}:m{mic}")
        lag = (20, 17, 19, 21)[mic] if label == "initial" else (21, 18, 20, 22)[mic]
        return (lag, lag, lag)

    def adapt(self, _stimulus, _directory) -> None:
        self.events.append("adapted")

    def product(self, *_args):
        if self.fail_product:
            raise ValueError("suppression failed")
        return ProductResult(0.0, 15.84, (26.67, 24.24), (12.33, 9.76), 0)

    def close(self, _dev) -> None:
        self.events.append("device_closed")

    def reconcile(self) -> None:
        self.events.append("reconciled")


def test_commissioning_resets_once_publishes_k_after_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    io = _FakeIO()
    marker = tmp_path / "active"
    artifact_path = tmp_path / "alignment.json"
    real_publish = aec_commission.atomic_write_json

    def publish(*args, **kwargs):
        io.events.append("published")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(aec_commission, "atomic_write_json", publish)

    artifact = aec_commission.run_commissioning(
        io,
        marker_path=marker,
        artifact_path=artifact_path,
        effective_uid=0,
    )

    assert artifact.k_samples == 245
    assert f"apply:{xvf3800.CHIP_AEC_SYS_DELAY_DEFAULT}:0" in io.events
    assert io.events.count("discarded_warmup") == 1
    timing = [event for event in io.events if event.startswith("timing:")]
    assert timing == [
        *(f"timing:initial:m{mic}" for mic in range(4)),
        *(f"timing:final:m{mic}" for mic in range(4)),
    ]
    assert io.events.index("discarded_warmup") < io.events.index(timing[0])
    assert io.events.count("reset_once") == 1
    assert io.events.index("device_closed") < io.events.index("volume_restored")
    assert io.events.index("volume_restored") < io.events.index("published")
    assert io.events[-1] == "reconciled"
    assert not marker.exists()
    assert artifact_from_dict(__import__("json").loads(artifact_path.read_text())) == artifact


def test_failed_evidence_preserves_old_artifact_and_restores_lifecycle(
    tmp_path: Path,
) -> None:
    io = _FakeIO(fail_product=True)
    marker = tmp_path / "active"
    artifact_path = tmp_path / "alignment.json"
    artifact_path.write_text("old-known-good\n")

    with pytest.raises(ValueError, match="suppression"):
        aec_commission.run_commissioning(
            io,
            marker_path=marker,
            artifact_path=artifact_path,
            effective_uid=0,
        )

    assert artifact_path.read_text() == "old-known-good\n"
    assert "device_closed" in io.events
    assert "volume_restored" in io.events
    assert io.events[-1] == "reconciled"
    assert not marker.exists()


def test_commissioning_requires_root_before_creating_marker(tmp_path: Path) -> None:
    marker = tmp_path / "active"

    with pytest.raises(aec_commission.CommissioningError, match="root"):
        aec_commission.run_commissioning(
            _FakeIO(), marker_path=marker, effective_uid=1000
        )

    assert not marker.exists()


def test_help_exits_without_starting_commissioning(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        aec_commission,
        "run_commissioning",
        lambda: pytest.fail("help must not start commissioning"),
    )

    with pytest.raises(SystemExit) as raised:
        aec_commission.main(["--help"])

    assert raised.value.code == 0
    assert "usage: jasper-aec-commission" in capsys.readouterr().out


def test_reset_rejects_a_different_xvf_after_reenumeration(monkeypatch) -> None:
    class Device:
        def __init__(self, serial: str) -> None:
            self.serial = serial
            self.closed = False

        def factory_serial(self) -> str:
            return self.serial

        def reboot_once(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    old = Device("XVF3800-001")
    replacement = Device("XVF3800-002")
    devices = iter((old, replacement))
    fake_xvf = types.ModuleType("jasper.xvf")
    fake_xvf.xvf_host = types.SimpleNamespace(find=lambda: next(devices))
    monkeypatch.setitem(sys.modules, "jasper.xvf", fake_xvf)
    exists = iter((False, False, True))

    class CardPath:
        def __init__(self, _value: str) -> None:
            pass

        def __truediv__(self, _value: str):
            return self

        def exists(self) -> bool:
            return next(exists)

    monkeypatch.setattr(aec_commission, "Path", CardPath)

    runtime = aec_commission.SystemIO()
    with pytest.raises(
        aec_commission.CommissioningError,
        match="physical identity changed",
    ):
        runtime.reset_once(_FakeIO().hardware)

    assert old.closed is True
    assert replacement.closed is True


def test_final_timing_must_land_on_bulls_eye() -> None:
    evidence = tuple(
        MicTiming(mic, 0, (lag, lag, lag))
        for mic, lag in enumerate((10, 11, 12, 13))
    )
    with pytest.raises(aec_commission.CommissioningError, match="center"):
        aec_commission._final_timing(evidence)


def test_final_timing_rejects_causal_window_cusp() -> None:
    evidence = tuple(
        MicTiming(mic, 0, (lag, lag, lag))
        for mic, lag in enumerate((1, 2, 3, 4))
    )
    with pytest.raises(aec_commission.CommissioningError, match="margin"):
        aec_commission._final_timing(evidence)
