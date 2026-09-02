# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from jasper import chip_aec_shipped_alignment as shipped
from jasper.chip_aec_alignment import (
    AlignmentArtifact,
    AlignmentIdentity,
    MicTiming,
    ProductResult,
    TimingRejected,
    TimingResult,
    artifact_from_dict,
)
from jasper.cli import aec_commission
from jasper.mics import xvf3800

ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = ROOT / "deploy/systemd/jasper-aec-commission.service"


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
    def __init__(
        self, *, fail_product: bool = False, dac_id: str = "apple_usb_c_dongle"
    ) -> None:
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
            dac_id,
            "usb-serial:DWH53530FLL2FN3A3",
            "single:outputd_dac",
            "S16_LE",
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

    # Three windows, all DISTINCT: preflight, the one taken before the final
    # timing, and the one K is built from. Degenerate windows (identical
    # before/after, both length 8) let a constant-zero delta, a sign flip, and
    # a before-echoes-after all read as correct — the +/-2 cross-check is the
    # only place K's median term meets a second measurement, so its
    # instrumentation has to be pinned by value.
    PREFLIGHT_SAMPLES = (282,) * 9
    BEFORE_SAMPLES = (282, 283, 284, 283, 283, 283, 283, 282, 284, 283, 283)
    AFTER_SAMPLES = (284, 285, 286, 285, 285, 285, 285, 284, 286, 285, 285, 285, 285)

    def queue(self, _hardware):
        self.queue_calls += 1
        samples = (
            self.PREFLIGHT_SAMPLES,
            self.BEFORE_SAMPLES,
            self.AFTER_SAMPLES,
        )[min(self.queue_calls, 3) - 1]
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

    def reconcile(self, *, reason: str = "chip-aec-commission") -> None:
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
        state_path=tmp_path / "commission-state.json",
        effective_uid=0,
    )

    # K = commissioned SYS_DELAY + median(the window K is built from).
    assert artifact.k_samples == -38 + 285
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
    outcome = json.loads((tmp_path / "commission-state.json").read_text())
    assert outcome["state"] == "passed"
    assert outcome["detail"] == ""


def test_passed_evidence_records_the_queue_cross_check_margin(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    # `abs(queue_median - before_median) > 2` is the only place K's median term
    # is cross-checked against a second measurement, and it is the tightest
    # tolerance in the run. A spread means nothing without the count it came
    # from, and a tolerance means nothing without the margin it passed by — so
    # the record has to carry both windows' medians and their difference.
    caplog.set_level("INFO", logger="jasper.aec_commission")
    io = _FakeIO()

    artifact = aec_commission.run_commissioning(
        io,
        marker_path=tmp_path / "active",
        artifact_path=tmp_path / "alignment.json",
        effective_uid=0,
    )

    assert "event=chip_aec_commission.passed" in caplog.text
    # VALUES, including the sign of the delta: a constant zero, a flipped sign,
    # and a before that echoes the after all satisfy a label-only assertion,
    # and all three would misreport the one margin this check passed by.
    for field in (
        "queue_median=285",
        "queue_median_before=283",
        "queue_median_delta=2",
        f"queue_samples={len(_FakeIO.AFTER_SAMPLES)}",
        "queue_spread=2",
    ):
        assert field in caplog.text, field
    assert "queue_median_delta=-2" not in caplog.text
    assert len(_FakeIO.AFTER_SAMPLES) != len(_FakeIO.BEFORE_SAMPLES)
    # The commissioned delay travels in the artifact so boot can bound against
    # it; it is the same number the evidence reports.
    assert artifact.sys_delay == -38
    assert f"sys_delay={artifact.sys_delay}" in caplog.text


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
            state_path=tmp_path / "commission-state.json",
            effective_uid=0,
        )

    assert artifact_path.read_text() == "old-known-good\n"
    assert "device_closed" in io.events
    assert "volume_restored" in io.events
    assert io.events[-1] == "reconciled"
    assert not marker.exists()
    # The failure reason is persisted for /aec's `commission` object — a
    # rejected run must be visible on the page, not only in the journal.
    outcome = json.loads((tmp_path / "commission-state.json").read_text())
    assert outcome["state"] == "failed"
    assert outcome["detail"] == "suppression failed"


class _RejectingIO(_FakeIO):
    """`_FakeIO` whose first timing trial writes a capture and is then refused."""

    def timing(self, _dev, _hardware, mic, _stimulus, _reference, directory, label):
        (directory / f"{label}-m{mic}-0.wav").write_bytes(b"RIFF")
        raise TimingRejected(
            TimingResult(20, 0.31, 0.42, 121, 0.41, -30.0, -20.0, 0), at_edge=False
        )


def test_a_timing_rejection_retains_its_captures_and_reports_both_arrivals(
    tmp_path: Path, caplog
) -> None:
    # Every rejected run used to unwind the captures that would explain it
    # (#3271), leaving a ratio in the journal and nothing to analyze.
    caplog.set_level("INFO", logger="jasper.aec_commission")
    rejections = tmp_path / "rejections"

    with pytest.raises(aec_commission.CommissioningError) as rejected:
        aec_commission.run_commissioning(
            _RejectingIO(),
            marker_path=tmp_path / "active",
            artifact_path=tmp_path / "alignment.json",
            state_path=tmp_path / "commission-state.json",
            rejection_dir=rejections,
            effective_uid=0,
        )

    retained = sorted(rejections.iterdir())
    assert [path.name for path in retained[0].iterdir()] == ["initial-m0-0.wav"]
    # Both arrivals and the ratio that separates them, in the journal and in
    # the operator-facing failure alike; the ratio is the run's whole verdict
    # and 121 - 20 samples is what a reflection hypothesis is tested against.
    for field in (
        "lag=20",
        "competitor_lag=121",
        "competitor_offset_ms=6.31",
        "peak_ratio=1.0244",
        f"retained_captures={retained[0]}",
    ):
        assert field in caplog.text, field
        assert field in str(rejected.value), field
    assert json.loads((tmp_path / "commission-state.json").read_text())["state"] == "failed"


def test_only_the_newest_rejections_are_retained(tmp_path: Path) -> None:
    root = tmp_path / "rejections"
    # Stamped ahead of this run: the box has no RTC, so the newest captures are
    # the ones a restored clock names oldest, and they are what the owner wants.
    for stamp in ("20270101T000000Z", "20270102T000000Z", "20270103T000000Z"):
        (root / stamp).mkdir(parents=True)
    directory = tmp_path / "run"
    directory.mkdir()
    for name in ("final-m1-2.wav", "aec-on.wav", "aec-off.wav", "adaptation.wav"):
        (directory / name).write_bytes(b"RIFF")

    destination = aec_commission._retain_rejected_captures(directory, root)

    assert destination is not None
    # The product pair is as much a rejected run's evidence as the timing
    # trials; the adaptation file it was recorded against is not.
    assert sorted(path.name for path in destination.iterdir()) == [
        "aec-off.wav",
        "aec-on.wav",
        "final-m1-2.wav",
    ]
    assert (directory / "adaptation.wav").exists()
    kept = sorted(path.name for path in root.iterdir())
    assert len(kept) == aec_commission.RETAINED_REJECTIONS
    assert destination.name in kept
    assert "20270101T000000Z" not in kept


def _supported_xvf() -> xvf3800.RuntimeProfile:
    return xvf3800.RuntimeProfile(
        present=True,
        variant=xvf3800.VARIANT_6CH,
        alsa_card_name="Array",
        capture_channels=6,
        chip_beam_plan=xvf3800.SQUARE_FIXED_150_210_PLAN,
        reason="",
    )


class _ProductionDetectIO(_FakeIO):
    """`_FakeIO` wearing the production `detect`, so its refusals are real."""

    def __init__(
        self, *, dac_id: str = "apple_usb_c_dongle", env_dac_id: str | None = None
    ) -> None:
        super().__init__(dac_id=dac_id)
        # Production reads one env value, but a fabricated identity's output_id
        # may not be empty (AlignmentIdentity forbids it), so the unset case
        # needs its own knob.
        self.env_dac_id = dac_id if env_dac_id is None else env_dac_id

    def detect(self):
        runtime = aec_commission.SystemIO()
        runtime.env = {"JASPER_AUDIO_DAC_ID": self.env_dac_id}
        return runtime.detect()


@pytest.mark.parametrize(
    "dac_id, qualification",
    [
        ("apple_usb_c_dongle", "approved"),
        ("hifiberry_dac8x_studio", "needs_calibration"),
    ],
)
def test_a_registered_dac_commissions_and_the_record_carries_its_qualification(
    tmp_path: Path, caplog, capsys, monkeypatch, dac_id: str, qualification: str
) -> None:
    # The doctor's own remedy for a parked box is this command, so refusing to
    # run it on an unqualified DAC left the household with no exit (#2984). The
    # run proceeds and the outcome carries the DAC's registry verdict instead.
    caplog.set_level("INFO", logger="jasper.aec_commission")
    monkeypatch.setattr(
        aec_commission.xvf3800, "detect_runtime_profile", _supported_xvf
    )

    aec_commission.run_commissioning(
        _ProductionDetectIO(dac_id=dac_id),
        marker_path=tmp_path / "active",
        artifact_path=tmp_path / "alignment.json",
        effective_uid=0,
    )

    assert f"dac_qualification={qualification}" in caplog.text
    # Named, not merely flagged: the operator has to learn WHICH profile made
    # the alignment provisional, and only then.
    assert (dac_id in capsys.readouterr().out) is (qualification != "approved")


@pytest.mark.parametrize("dac_id", ["", "dac_nobody_has_codified"])
def test_an_unregistered_dac_is_refused_before_anything_is_disturbed(
    tmp_path: Path, monkeypatch, dac_id: str
) -> None:
    # Registry membership is not qualification. `output_hardware_key` refuses an
    # unknown output edge outright — a deliberate don't-guess park — so the
    # commissioner must refuse it too, and BEFORE the volume move, the service
    # stop, and the XVF reset that would otherwise buy the same dead end.
    monkeypatch.setattr(
        aec_commission.xvf3800, "detect_runtime_profile", _supported_xvf
    )
    io = _ProductionDetectIO(env_dac_id=dac_id)
    artifact_path = tmp_path / "alignment.json"

    with pytest.raises(aec_commission.CommissioningError, match="registry"):
        aec_commission.run_commissioning(
            io,
            marker_path=tmp_path / "active",
            artifact_path=artifact_path,
            effective_uid=0,
        )

    assert io.events == ["idle", "reconciled"]
    assert not artifact_path.exists()


def test_commissioning_arms_reference_vector_between_stop_and_measurement(
    tmp_path: Path,
) -> None:
    # The reconciler routes into its reference-vector arm only for the
    # reason-keyed call while the marker is LIVE, so the arm reconcile must
    # carry both — after the services stop (a plain outputd start, not a
    # bounce) and before any measurement; the cleanup reconcile must run
    # after the marker is removed so it restores the resting vector.
    marker = tmp_path / "active"

    class MarkerRecordingIO(_FakeIO):
        def reconcile(self, *, reason: str = "chip-aec-commission") -> None:
            self.events.append(
                f"reconciled:marker={int(marker.exists())}:reason={reason}"
            )

    io = MarkerRecordingIO()

    aec_commission.run_commissioning(
        io,
        marker_path=marker,
        artifact_path=tmp_path / "alignment.json",
        state_path=tmp_path / "commission-state.json",
        effective_uid=0,
    )

    arm = f"reconciled:marker=1:reason={aec_commission.ARM_RECONCILE_REASON}"
    assert arm in io.events
    assert io.events.index("services_stopped") < io.events.index(arm)
    assert io.events.index(arm) < io.events.index("reset_once")
    assert io.events[-1] == "reconciled:marker=0:reason=chip-aec-commission"


def test_commission_outcome_path_matches_control_reader() -> None:
    # jasper-control duplicates the literal (importing this module would pull
    # numpy into the long-lived daemon); the two must name one file.
    from jasper.control import aec_endpoints

    assert aec_endpoints._AEC_COMMISSION_STATE_FILE == str(aec_commission.STATE_PATH)


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


def test_emitting_a_class_entry_reads_the_artifact_and_commissions_nothing(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    # The harvest step: it prints source the owner pastes into the shipped
    # registry, and touches nothing else on the box it runs on.
    monkeypatch.setattr(
        aec_commission,
        "run_commissioning",
        lambda: pytest.fail("emitting a class entry must not commission"),
    )
    artifact = AlignmentArtifact(_FakeIO().live_identity, 245, -38)
    path = tmp_path / "chip-aec-alignment.json"
    path.write_text(json.dumps(artifact.to_dict()), encoding="utf-8")

    assert aec_commission.main(["--emit-class-entry", str(path)]) == 0

    row = eval(
        capsys.readouterr().out, {"ShippedAlignment": shipped.ShippedAlignment}
    )
    assert (row.k_samples, row.sys_delay) == (245, -38)
    monkeypatch.setattr(shipped, "REGISTRY", (row,))
    assert shipped.for_identity(artifact.identity) is row

    # An unreadable artifact is a message, not a traceback.
    assert aec_commission.main(["--emit-class-entry", str(tmp_path / "absent")]) == 1

    # An unset shell variable arrives as an empty path. That is still the
    # harvest path: falling through to a commissioning run would stop the
    # services and play sweeps at someone who asked to print a registry row.
    assert aec_commission.main(["--emit-class-entry", ""]) == 1


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


def test_sandboxed_unit_keeps_the_reconciler_leg_env_path_writable() -> None:
    """A ProtectSystem= sandbox must not lock out the arm reconcile's own
    writes: it persists the leg env under /etc/jasper, so the unit needs
    ReadWritePaths=/etc/jasper whenever it sandboxes with ProtectSystem=."""
    text = UNIT_PATH.read_text(encoding="utf-8")
    if "ProtectSystem=" in text:
        assert "ReadWritePaths=/etc/jasper" in text, (
            "jasper-aec-commission.service sandboxes with ProtectSystem= but "
            "is missing ReadWritePaths=/etc/jasper, which the arm reconcile "
            "needs writable for its leg-env writes"
        )
