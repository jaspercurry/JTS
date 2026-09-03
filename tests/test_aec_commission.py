# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import math
import sys
import threading
import types
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

import pytest

from jasper import chip_aec_shipped_alignment as shipped
from jasper.active_speaker.calibration_level import AUDIBLE_RAMP_STEP_DB
from jasper.chip_aec_alignment import (
    AlignmentArtifact,
    AlignmentIdentity,
    LevelProbe,
    MIC_COUNT,
    MicTiming,
    ProductResult,
    Rejected,
    TARGET_RAW_EXCESS_SNR_DB,
    TIMING_TRIALS,
    TimingRejected,
    TimingResult,
    artifact_from_dict,
    commissioning_stimulus,
)
from jasper.cli import aec_commission
from jasper.correction import coordinator
from jasper.correction.coordinator import MeasurementWindowError
from jasper.mics import xvf3800

ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = ROOT / "deploy/systemd/jasper-aec-commission.service"


@pytest.fixture(autouse=True)
def gate(monkeypatch) -> list[dict]:
    """Record what every run asks of `measurement_window`, and hold nothing."""

    calls: list[dict] = []

    @asynccontextmanager
    async def window(**kwargs):
        calls.append({"action": "acquire", **kwargs})
        try:
            yield
        finally:
            calls.append({"action": "release", "gate_owner": kwargs["gate_owner"]})

    monkeypatch.setattr(coordinator, "measurement_window", window)
    return calls


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
        # jts.local's own run 4: the room measured 5.45 dB of raw excess SNR
        # at MEASUREMENT_VOLUME_DB, well under the product gate's floor.
        self.level_probe = LevelProbe(5.45, 0, -60.0, -55.0)
        self.lease = aec_commission.IsolationLease(
            aec_commission.COMMISSION_GATE_OWNER
        )

    def audio_isolation(self) -> aec_commission.IsolationLease:
        return self.lease

    def wait_reconciler_idle(self) -> None:
        self.events.append("idle")

    def detect(self):
        return self.hardware

    def prepare_volume(self) -> float:
        self.events.append("volume_set")
        return -20.0

    def set_volume(self, db: float) -> None:
        self.events.append(f"volume_measurement:{db:.4f}")

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

    def warmup(
        self, _hardware, _stimulus, _active, directory, _lease
    ) -> LevelProbe:
        (directory / aec_commission.WARMUP_CAPTURE_NAME).write_bytes(b"RIFF")
        self.events.append("level_warmup")
        return self.level_probe

    def timing(
        self, _dev, _hardware, mic: int, _stimulus, _reference, _directory, label,
        _lease,
    ) -> tuple[int, ...]:
        self.events.append(f"timing:{label}:m{mic}")
        lag = (20, 17, 19, 21)[mic] if label == "initial" else (21, 18, 20, 22)[mic]
        return (lag,) * TIMING_TRIALS

    # The real loop over a fake chip: it only needs `adapt` and `convergence`,
    # so the cap and the refusal under test are the shipped ones.
    adapt_until_converged = aec_commission.SystemIO.adapt_until_converged

    def adapt(self, _stimulus) -> None:
        self.events.append("adapted")

    def product(self, _dev, _hardware, _delay, _stimulus, _active, directory, _lease):
        # Two captures, as the shipped method plays: AEC on, then bypassed.
        for name in ("aec-on.wav", "aec-off.wav"):
            (directory / name).write_bytes(b"RIFF")
            self.events.append(f"product_capture:{name}")
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
    assert io.events.count("level_warmup") == 1
    timing = [event for event in io.events if event.startswith("timing:")]
    assert timing == [
        *(f"timing:initial:m{mic}" for mic in range(4)),
        *(f"timing:final:m{mic}" for mic in range(4)),
    ]
    assert io.events.index("level_warmup") < io.events.index(timing[0])
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
    rejections = tmp_path / "rejections"
    artifact_path.write_text("old-known-good\n")

    with pytest.raises(aec_commission.CommissioningError):
        aec_commission.run_commissioning(
            io,
            marker_path=marker,
            artifact_path=artifact_path,
            state_path=tmp_path / "commission-state.json",
            rejection_dir=rejections,
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
    # A refusal the analyzer raised without evidence fields is exactly the one
    # whose captures have to survive, and the record has to name where.
    retained = sorted(rejections.iterdir())
    assert sorted(path.name for path in retained[0].iterdir()) == [
        "aec-off.wav",
        "aec-on.wav",
        aec_commission.WARMUP_CAPTURE_NAME,
    ]
    assert str(retained[0]) in outcome["detail"]


@pytest.mark.parametrize(
    "probe, expected_offset_db",
    [
        # Run 4 (too quiet), on target, loud enough to come back DOWN, and a
        # clipped capture — which understates its own excitation.
        (LevelProbe(5.45, 0, -60.0, -55.0), 9.55),
        (LevelProbe(TARGET_RAW_EXCESS_SNR_DB, 0, -60.0, -55.0), 0.0),
        (LevelProbe(25.0, 0, -60.0, -55.0), -10.0),
        (LevelProbe(5.45, 12, -60.0, -55.0), 0.0),
        # HEARING: a whole audible step or more from the target is refused,
        # never commanded in one jump and never silently under-shot.
        (LevelProbe(2.0, 0, -60.0, -55.0), None),
    ],
)
def test_the_warmup_probe_sets_one_capped_measurement_level(
    tmp_path: Path, caplog, probe: LevelProbe, expected_offset_db: float | None
) -> None:
    caplog.set_level("INFO", logger="jasper.aec_commission")
    io = _FakeIO()
    io.level_probe = probe
    rejections = tmp_path / "rejections"
    refused = expected_offset_db is None
    offset = AUDIBLE_RAMP_STEP_DB if refused else expected_offset_db
    volume = aec_commission.MEASUREMENT_VOLUME_DB + offset

    with (
        pytest.raises(aec_commission.CommissioningError)
        if refused
        else nullcontext()
    ) as rejection:
        aec_commission.run_commissioning(
            io,
            marker_path=tmp_path / "active",
            artifact_path=tmp_path / "alignment.json",
            state_path=tmp_path / "commission-state.json",
            rejection_dir=rejections,
            effective_uid=0,
        )

    writes = [event for event in io.events if event.startswith("volume_measurement:")]
    record = str(rejection.value) if refused else caplog.text
    for field in (
        f"warmup_raw_excess_snr_db={round(probe.raw_excess_snr_db, 2)}",
        f"target_raw_excess_snr_db={TARGET_RAW_EXCESS_SNR_DB}",
        f"level_offset_db={round(offset, 2)}",
        f"selected_volume_db={round(volume, 2)}",
        # Which guard dominates says whether the dB-for-dB assumption held.
        "pre_guard_dbfs=-60.0",
        "post_guard_dbfs=-55.0",
    ):
        assert field in record, field
    if refused:
        assert writes == []
        assert f"step_cap_db={AUDIBLE_RAMP_STEP_DB}" in record
        retained = sorted(rejections.iterdir())
        assert [path.name for path in retained[0].iterdir()] == [
            aec_commission.WARMUP_CAPTURE_NAME
        ]
        assert "volume_restored" in io.events
        return
    assert writes == [f"volume_measurement:{volume:.4f}"]
    order = [io.events.index(e) for e in ("level_warmup", writes[0], "timing:initial:m0")]
    assert order == sorted(order)


class _RejectingIO(_FakeIO):
    """`_FakeIO` whose first timing trial writes a capture and is then refused."""

    def timing(
        self, _dev, _hardware, mic, _stimulus, _reference, directory, label, _lease,
    ):
        (directory / f"{label}-m{mic}-0.wav").write_bytes(b"RIFF")
        raise TimingRejected(
            TimingResult(20, 0.31, 0.42, 121, 0.41, 8, 0.41, -30.0, -20.0, 0),
            at_edge=False,
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
    assert sorted(path.name for path in retained[0].iterdir()) == [
        "initial-m0-0.wav",
        aec_commission.WARMUP_CAPTURE_NAME,
    ]
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


class _ProductRejectingIO(_FakeIO):
    """`_FakeIO` whose product pair is captured and then refused by the gate."""

    def product(
        self, _dev, _hardware, _delay, _stimulus, _active, directory, _lease,
    ):
        super().product(
            _dev, _hardware, _delay, _stimulus, _active, directory, _lease
        )
        raise Rejected(
            "product",
            ProductResult(0.2, 15.84, (6.2, 24.24), (12.33, 9.76), 0).evidence(),
        )


def test_a_product_rejection_retains_its_pair_and_reports_the_threshold(
    tmp_path: Path, caplog
) -> None:
    # The five-threshold block refused with a bare message, so the run that
    # passed timing and converged left neither the measurement, the number it
    # was held to, nor the two captures behind (#3271).
    caplog.set_level("INFO", logger="jasper.aec_commission")
    rejections = tmp_path / "rejections"

    with pytest.raises(aec_commission.CommissioningError) as rejected:
        aec_commission.run_commissioning(
            _ProductRejectingIO(),
            marker_path=tmp_path / "active",
            artifact_path=tmp_path / "alignment.json",
            state_path=tmp_path / "commission-state.json",
            rejection_dir=rejections,
            effective_uid=0,
        )

    retained = sorted(rejections.iterdir())
    assert sorted(path.name for path in retained[0].iterdir()) == [
        "aec-off.wav",
        "aec-on.wav",
        aec_commission.WARMUP_CAPTURE_NAME,
    ]
    # The measurement that failed and the number it was held to, in the
    # operator-facing refusal and the journal alike.
    for field in (
        "beam_suppression_db=(6.2, 24.24)",
        "min_beam_suppression_db=10.0",
        f"retained_captures={retained[0]}",
    ):
        assert field in str(rejected.value), field
    for field in (
        "event=chip_aec_commission.product_rejected",
        "phase=product",
        "min_beam_suppression_db=10.0",
    ):
        assert field in caplog.text, field
    assert json.loads((tmp_path / "commission-state.json").read_text())["state"] == "failed"


class _NeverConvergingIO(_FakeIO):
    """`_FakeIO` that captures timing, plays the train, and never converges."""

    def timing(
        self, dev, hardware, mic, stimulus, reference, directory, label, lease,
    ):
        (directory / f"{label}-m{mic}-0.wav").write_bytes(b"RIFF")
        return super().timing(
            dev, hardware, mic, stimulus, reference, directory, label, lease
        )

    def convergence(self, _dev) -> int:
        return 0


def test_a_run_that_never_converges_retains_its_captures_and_its_counts(
    tmp_path: Path, caplog
) -> None:
    # The convergence refusal raised straight out of `_commission`, so a run
    # that adapted and never converged unwound the timing captures that would
    # explain it -- the same loss the gate verdicts already fixed (#3271).
    caplog.set_level("INFO", logger="jasper.aec_commission")
    rejections = tmp_path / "rejections"

    with pytest.raises(aec_commission.CommissioningError) as rejected:
        aec_commission.run_commissioning(
            _NeverConvergingIO(),
            marker_path=tmp_path / "active",
            artifact_path=tmp_path / "alignment.json",
            state_path=tmp_path / "commission-state.json",
            rejection_dir=rejections,
            effective_uid=0,
        )

    retained = sorted(rejections.iterdir())
    assert sorted(path.name for path in retained[0].iterdir()) == [
        *(
            f"{phase}-m{mic}-0.wav"
            for phase in ("final", "initial")
            for mic in range(4)
        ),
        aec_commission.WARMUP_CAPTURE_NAME,
    ]
    for field in (
        f"chunks_played={aec_commission.ADAPTATION_CHUNKS}",
        "converged=0",
        "phase=adaptation",
        f"retained_captures={retained[0]}",
    ):
        assert field in str(rejected.value), field
    for field in ("event=chip_aec_commission.adaptation_rejected", "adaptation_seconds="):
        assert field in caplog.text, field


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


def test_the_window_is_taken_with_commissionings_own_registered_owner(
    gate: list[dict],
) -> None:
    # mux.FANIN_TEST_OWNERS is a CLOSED allowlist, and voice is already stopped
    # by STOP_UNITS, so there is no daemon left to pause.
    from jasper.mux import FANIN_TEST_OWNERS

    assert aec_commission.COMMISSION_GATE_OWNER in FANIN_TEST_OWNERS
    lease = aec_commission.SystemIO().audio_isolation()

    lease.start()
    lease.stop()

    assert gate == [
        {
            "action": "acquire",
            "gate_owner": "chip-aec-commission",
            "skip_voice_pause": True,
        },
        {"action": "release", "gate_owner": "chip-aec-commission"},
    ]


def test_a_release_racing_the_hold_is_honoured_rather_than_dropped(
    gate: list[dict],
) -> None:
    # A signal can unwind `start()` before `hold` owns the asyncio.Event that
    # `release` wakes it through. Driven straight against HeldWindow, and NOT
    # through IsolationLease.start()/stop(), because that is the only way to
    # force the interleave deterministically: release first, loop second.
    # Without the flag `release` sets, nothing ever wakes the hold and the
    # lease's join parks until its timeout.
    window = coordinator.HeldWindow(
        gate_owner=aec_commission.COMMISSION_GATE_OWNER, skip_voice_pause=True
    )
    window.release()

    # daemon: a regression leaves this hold parked forever, and the assertion
    # below should be what reports that rather than a hang at interpreter exit.
    holder = threading.Thread(target=lambda: asyncio.run(window.hold()), daemon=True)
    holder.start()
    holder.join(timeout=5)

    assert not holder.is_alive()
    assert window.error is None
    assert [call["action"] for call in gate] == ["acquire", "release"]


async def _cancel_when_set(trip: threading.Event, owner) -> None:
    while not trip.is_set():
        await asyncio.sleep(0.005)
    owner.cancel()


class _LosingIsolationIO(_FakeIO):
    """`_FakeIO` that plays on while mux drops the diagnostic gate under it."""

    def __init__(self, lose_at: str) -> None:
        super().__init__()
        self.lose_at = lose_at
        self.abort = threading.Event()

    def _lose(self, step: str) -> None:
        if step != self.lose_at:
            return
        self.abort.set()
        self.lease.lost.wait(timeout=5)

    def warmup(self, hardware, stimulus, active, directory, lease) -> LevelProbe:
        probe = super().warmup(hardware, stimulus, active, directory, lease)
        self._lose("warmup")
        return probe

    def timing(
        self, dev, hardware, mic, stimulus, reference, directory, label, lease,
    ):
        lags = super().timing(
            dev, hardware, mic, stimulus, reference, directory, label, lease
        )
        if mic == 0:
            self._lose("timing")
        return lags

    def convergence(self, dev) -> int:
        # Never converge, so the adaptation train is stopped by the per-chunk
        # isolation check rather than by the chip agreeing to finish.
        return 0 if self.lose_at == "adapt" else super().convergence(dev)

    def adapt(self, stimulus) -> None:
        super().adapt(stimulus)
        self._lose("adapt")

    def product(self, dev, hardware, delay, stimulus, active, directory, lease):
        result = super().product(
            dev, hardware, delay, stimulus, active, directory, lease
        )
        self._lose("product")
        return result


# `warmup` loses it before a gated phase; `timing` and `adapt` lose it INSIDE
# one, where only the per-capture and per-chunk checks can stop it; `product`
# loses it after the last phase, where only the check in front of publication
# can.
@pytest.mark.parametrize("lose_at", ["warmup", "timing", "adapt", "product"])
def test_isolation_lost_mid_run_publishes_nothing_and_hands_the_music_back(
    tmp_path: Path, monkeypatch, gate: list[dict], lose_at: str
) -> None:
    # The window aborts by cancelling whoever entered it, so a lost mux lease
    # has to reach the body: a sweep that shared the speaker with the household
    # must never become this box's published alignment.
    io = _LosingIsolationIO(lose_at)

    @asynccontextmanager
    async def aborting_window(**kwargs):
        gate.append({"action": "acquire", **kwargs})
        watch = asyncio.create_task(
            _cancel_when_set(io.abort, asyncio.current_task())
        )
        try:
            yield
        except asyncio.CancelledError as exc:
            raise MeasurementWindowError("isolation could not be renewed") from exc
        finally:
            watch.cancel()
            gate.append({"action": "release", "gate_owner": kwargs["gate_owner"]})

    monkeypatch.setattr(coordinator, "measurement_window", aborting_window)
    artifact_path = tmp_path / "alignment.json"
    state_path = tmp_path / "commission-state.json"

    with pytest.raises(aec_commission.CommissioningError):
        aec_commission.run_commissioning(
            io,
            marker_path=tmp_path / "active",
            artifact_path=artifact_path,
            state_path=state_path,
            rejection_dir=tmp_path / "rejections",
            effective_uid=0,
        )

    assert io.lease.lost.is_set()
    assert not artifact_path.exists()
    assert json.loads(state_path.read_text())["state"] == "failed"
    assert [call["action"] for call in gate] == ["acquire", "release"]
    # And it stops chirping at the next capture or chunk rather than sweeping
    # on over whatever the household is playing.
    captures = len([event for event in io.events if event.startswith("timing:")])
    every_capture = 2 * MIC_COUNT
    assert (captures, io.events.count("adapted")) == {
        "warmup": (0, 0),
        # These fakes stand in for a whole `SystemIO.timing`/`product` call, so
        # what stops them is the next phase boundary. The real per-capture
        # guard lives on `_capture` and is pinned separately below.
        "timing": (MIC_COUNT, 0),
        "adapt": (every_capture, 1),
        "product": (every_capture, 1),
    }[lose_at]


def test_no_capture_plays_once_isolation_is_lost() -> None:
    # `SystemIO.timing` plays TIMING_TRIALS captures per call and `product`
    # plays two, so a guard at the phase boundary alone would let the rest of
    # them out over the household. Every capture in this CLI is taken through
    # `_capture`, which is why the guard sits there.
    lease = aec_commission.IsolationLease(aec_commission.COMMISSION_GATE_OWNER)
    lease.lost.set()

    with pytest.raises(aec_commission.CommissioningError):
        # Nothing after the guard runs, so the arguments never get read.
        aec_commission.SystemIO()._capture(None, Path("in.wav"), Path("out.wav"), lease)


def test_a_release_failure_after_a_pass_is_its_own_event_not_a_failed_run(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    # Music stays gated until mux's 60 s lease lapses, so the operator has to
    # see it — but the sweep passed, and the artifact it published is good.
    caplog.set_level("INFO", logger="jasper.aec_commission")

    @asynccontextmanager
    async def stuck_release(**_kwargs):
        yield
        raise MeasurementWindowError("mux did not confirm the release")

    monkeypatch.setattr(coordinator, "measurement_window", stuck_release)
    io = _FakeIO()
    artifact_path = tmp_path / "alignment.json"
    state_path = tmp_path / "commission-state.json"

    artifact = aec_commission.run_commissioning(
        io,
        marker_path=tmp_path / "active",
        artifact_path=artifact_path,
        state_path=state_path,
        effective_uid=0,
    )

    assert artifact_from_dict(json.loads(artifact_path.read_text())) == artifact
    assert json.loads(state_path.read_text())["state"] == "passed"
    assert isinstance(io.lease.error, MeasurementWindowError)
    assert "chip_aec_commission.isolation_release_failed" in caplog.text


def test_a_refused_body_hands_the_music_back_once_after_its_own_cleanup(
    tmp_path: Path, gate: list[dict]
) -> None:
    # A signal unwinds `_commission` on the main thread like any other refusal:
    # its finally closes the device and restores the fader, and only then —
    # after the cleanup reconcile has restarted outputd — does the gate lift.
    io = _FakeIO(fail_product=True)

    with pytest.raises(aec_commission.CommissioningError):
        aec_commission.run_commissioning(
            io,
            marker_path=tmp_path / "active",
            artifact_path=tmp_path / "alignment.json",
            state_path=tmp_path / "commission-state.json",
            rejection_dir=tmp_path / "rejections",
            effective_uid=0,
        )

    assert [call["action"] for call in gate] == ["acquire", "release"]
    assert io.events.index("device_closed") < io.events.index("volume_restored")
    assert io.events[-1] == "reconciled"


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
        MicTiming(mic, 0, (lag,) * TIMING_TRIALS)
        for mic, lag in enumerate((10, 11, 12, 13))
    )
    with pytest.raises(aec_commission.CommissioningError, match="center"):
        aec_commission._final_timing(evidence)


def test_final_timing_rejects_causal_window_cusp() -> None:
    evidence = tuple(
        MicTiming(mic, 0, (lag,) * TIMING_TRIALS)
        for mic, lag in enumerate((1, 2, 3, 4))
    )
    with pytest.raises(aec_commission.CommissioningError, match="margin"):
        aec_commission._final_timing(evidence)


def test_a_full_run_stays_inside_the_chirp_budget() -> None:
    # The figure held to the budget is the worst case — every adaptation chunk
    # spent — because that is what a run that never converges costs.
    stereo, _reference, _active = commissioning_stimulus()

    assert (
        aec_commission.chirp_seconds(len(stereo))
        <= aec_commission.CHIRP_BUDGET_SECONDS
    )


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


def _stateful_camilladsp(
    monkeypatch, *, level_db: float, freeze_readback: bool = False
) -> dict[str, float]:
    """Install a fake `camilladsp` whose main volume remembers what was set.

    Returned so a test can read what actually LANDED at the fake hardware —
    the clamped value, when a caller asks above the ceiling.
    ``freeze_readback`` pins what `main_volume` reports regardless of writes,
    which is how a readback that never agrees is simulated.
    """
    state = {"db": level_db}
    client = types.SimpleNamespace(
        volume=types.SimpleNamespace(
            set_main_volume=lambda db: state.__setitem__("db", db),
            main_volume=lambda: level_db if freeze_readback else state["db"],
        ),
        connect=lambda: None,
        disconnect=lambda: None,
    )
    monkeypatch.setitem(
        sys.modules,
        "camilladsp",
        types.SimpleNamespace(CamillaClient=lambda _host, _port: client),
    )
    return state


def _commissioning_io(monkeypatch) -> aec_commission.SystemIO:
    """The real `SystemIO` with only the volume-ramp sleep stubbed; the fader
    path itself stays production. The owner install is the shape `main()`
    registers through `install_env_canonical_target_provider`."""
    from jasper.camilla import primary_controller
    from jasper.volume_owner import VolumeOwner, install_volume_owner

    fader = primary_controller()
    install_volume_owner(
        VolumeOwner(
            set_fader_db=lambda db: fader.set_volume_db(db, best_effort=True),
            get_fader_db=lambda: fader.get_volume_db(best_effort=True),
        )
    )
    io = aec_commission.SystemIO()
    monkeypatch.setattr(aec_commission.time, "sleep", lambda _seconds: None)
    return io


def test_commissioning_volume_round_trips_through_the_camilla_helper(
    monkeypatch,
) -> None:
    """`prepare_volume` ducks to the measurement level and hands back the
    household level; `restore_volume` puts that level back.

    Driven through the real `camilla.declare_main_volume_db` /
    `read_main_volume_db`, so this covers the helpers' move out of the retired
    `jasper-aec-tune` as well as the round trip.
    """
    state = _stateful_camilladsp(monkeypatch, level_db=-18.0)
    io = _commissioning_io(monkeypatch)

    original = io.prepare_volume()
    assert original == -18.0
    assert state["db"] == pytest.approx(aec_commission.MEASUREMENT_VOLUME_DB)

    io.restore_volume(original)
    assert state["db"] == pytest.approx(-18.0)


def test_a_commissioning_volume_above_the_ceiling_lands_clamped_and_refuses(
    monkeypatch,
) -> None:
    """One hardware door: the write reaches `_coerce_main_volume_db`, so a
    request above 0 dB lands AT 0 dB and the readback confirm then refuses,
    rather than the caller believing a level the speaker never played."""
    from jasper.camilla import CamillaVolumeError

    state = _stateful_camilladsp(monkeypatch, level_db=-18.0)
    io = _commissioning_io(monkeypatch)

    with pytest.raises(CamillaVolumeError):
        io.set_volume(6.0)

    assert state["db"] == 0.0


def test_a_non_finite_commissioning_fader_value_is_refused(monkeypatch) -> None:
    """No commissioning capture may start against a fader nobody can name.

    All three of the helpers' refusals, which is one behaviour at one
    altitude: the READ that comes back non-finite, the REQUEST that is
    non-finite, and the READBACK that stays non-finite after a write. Each
    raises rather than letting a run proceed at an unknown level.
    """
    from jasper.camilla import (
        CamillaVolumeError,
        declare_main_volume_db,
        read_main_volume_db,
    )

    _stateful_camilladsp(monkeypatch, level_db=math.nan, freeze_readback=True)
    io = _commissioning_io(monkeypatch)

    # The READ: what `prepare_volume` calls to learn the household level.
    with pytest.raises(CamillaVolumeError):
        read_main_volume_db()

    # The REQUEST: a caller asking for a level that is not a number.
    with pytest.raises(CamillaVolumeError):
        declare_main_volume_db(math.nan)

    # The READBACK, through the commissioning path: the write is accepted but
    # the fader never reports a level anyone can name.
    with pytest.raises(CamillaVolumeError):
        io.set_volume(-20.0)
