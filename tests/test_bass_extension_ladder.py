# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free coverage for the pure commissioning ladder (Wave 4 rev 9)."""

from __future__ import annotations

import ast
import importlib.util
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.bass_extension.adapters.base import CaptureRole, FitRefusal, MagnitudeCurve, TargetSpec
from jasper.bass_extension.limiter_evidence import (
    LimiterEvidenceRefusal,
    LimiterThresholdSet,
)
from jasper.bass_extension.profile import BassExtensionRefusal
from jasper.bass_extension.targets import MARGINS, AnchorPoint
from jasper.bass_extension import ladder as L
from jasper.bass_extension.ladder import (
    CaptureRecord,
    CeilingResult,
    CommissioningManifest,
    DryRunInputs,
    LadderError,
    LadderEvent,
    LadderManifestError,
    LadderSession,
    LadderState,
    LadderTransitionError,
    RungMeasurement,
    RungRecord,
    RungVerdict,
    apply_sustain,
    derive_anchor_set,
    evaluate_rung,
    preflight,
    run_ladder,
    start_session,
    sustain_result,
    synthetic_dry_run,
    synthetic_limiter_intake,
    transition,
    validate_commissioning_manifest,
)

# Reuse the frozen limiter producer's own valid-bundle builder (single source of
# truth for a passing evidence bundle) — proves no duplicated fixture math.  Load
# it by file path so the reuse does not depend on the sibling test being importable
# by name under any particular pytest import mode.
def _load_limiter_fixtures():
    path = Path(__file__).resolve().parent / "test_bass_extension_limiter_evidence.py"
    spec = importlib.util.spec_from_file_location("_bex_limiter_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_LEV = _load_limiter_fixtures()

FREQS = np.geomspace(12.0, 260.0, 500)
BASE = 2.0 * np.sin(np.log(FREQS) * 4.0)
NOISE = (FREQS, np.full_like(FREQS, -120.0))
FIRST_CMD = 0.0
DECISION_BAND = (20.0, 250.0)


def _aid(label: str) -> ArtifactIdentity:
    return ArtifactIdentity(
        bundle_kind="jts_bass_extension_commissioning_bundle",
        bundle_id="synthetic",
        relative_path=f"{label}.wav",
        sha256=("%064x" % (abs(hash(label)) % (16**64))),
        byte_size=1024,
    )


def _measure(ordinal, cmd, level, *, fund=None, harmonics=None, **flags) -> RungMeasurement:
    delta = cmd - FIRST_CMD
    fund_db = (BASE + delta) if fund is None else fund
    harm = harmonics if harmonics is not None else {2: (FREQS, fund_db - 60.0), 3: (FREQS, fund_db - 66.0)}
    return RungMeasurement(
        rung_ordinal=ordinal,
        commanded_main_volume_db=cmd,
        listening_level=level,
        capture_id=_aid(f"r{ordinal}"),
        fund_freqs=FREQS,
        fund_db=fund_db,
        harmonics=harm,
        predicted_db=BASE + delta,
        decision_band_hz=DECISION_BAND,
        noise_floor=NOISE,
        **flags,
    )


def _compression_fail_fund(cmd):
    fund = (BASE + (cmd - FIRST_CMD)).copy()
    fund[(FREQS >= 25.0) & (FREQS <= 70.0)] -= 6.0
    return fund


def _mic_moved_fund(cmd):
    fund = (BASE + (cmd - FIRST_CMD)).copy()
    mask = (FREQS >= 150.0) & (FREQS <= 400.0)
    fund[mask] = 4.0 * np.sin(np.log(FREQS[mask]) * 19.0) + (cmd - FIRST_CMD)
    return fund


def _thd_fail_harmonics(cmd):
    fund_db = BASE + (cmd - FIRST_CMD)
    return {2: (FREQS, fund_db - 3.0)}


NORMAL = MARGINS["normal"]

# --------------------------------------------------------------------------- #
# Transition table
# --------------------------------------------------------------------------- #

_FORWARD_ORDER = [
    LadderState.IDLE,
    LadderState.CHARACTERIZE,
    LadderState.FIT,
    LadderState.PROPOSE,
    LadderState.VERIFY_DEEPEST,
    LadderState.LADDER,
    LadderState.SUSTAIN_TEST,
    LadderState.DERIVE_ANCHORS,
    LadderState.REVIEW,
]


def _session(state=LadderState.IDLE) -> LadderSession:
    return replace(
        LadderSession(session_id="s", margin="normal", adapter_id="sealed_v1"), state=state
    )


def test_every_legal_forward_transition_advances_one_state():
    session = _session()
    for current, nxt in zip(_FORWARD_ORDER, _FORWARD_ORDER[1:]):
        assert session.state == current
        session = transition(session, LadderEvent(nxt.value))
    assert session.state == LadderState.REVIEW


def test_forward_skips_are_rejected():
    for src, bad in (
        (LadderState.IDLE, "fit"),
        (LadderState.CHARACTERIZE, "propose"),
        (LadderState.PROPOSE, "ladder"),
        (LadderState.LADDER, "review"),
    ):
        with pytest.raises(LadderTransitionError):
            transition(_session(src), LadderEvent(bad))


def test_review_has_no_forward_edge():
    assert L._FORWARD[LadderState.REVIEW] == frozenset()
    with pytest.raises(LadderTransitionError):
        transition(_session(LadderState.REVIEW), LadderEvent("review"))


def test_accepted_is_unreachable():
    assert "accepted" not in {state.value for state in LadderState}
    assert not any("accepted" in {d.value for d in dests} for dests in L._FORWARD.values())
    with pytest.raises(LadderTransitionError):
        transition(_session(LadderState.REVIEW), LadderEvent("accepted"))


def test_unknown_event_rejected():
    with pytest.raises(LadderTransitionError):
        transition(_session(), LadderEvent("nonsense"))


def test_updates_restricted_to_data_fields():
    with pytest.raises(LadderTransitionError):
        transition(_session(), LadderEvent("characterize", updates={"state": LadderState.FIT}))
    ok = transition(_session(), LadderEvent("characterize", updates={"captures": ()}))
    assert ok.state == LadderState.CHARACTERIZE


def test_abort_from_every_state():
    for state in LadderState:
        aborted = transition(_session(state), LadderEvent("abort"))
        assert aborted.state == LadderState.ABORTED
        if state is not LadderState.ABORTED:
            assert aborted.disposition == "operator_stop"


def test_abort_is_idempotent_on_terminal():
    already = _session(LadderState.ABORTED)
    assert transition(already, LadderEvent("abort")) is already


def test_abort_reason_override():
    aborted = transition(_session(LadderState.LADDER), LadderEvent("abort", reason="phone_clip"))
    assert aborted.disposition == "phone_clip"


def test_restart_retires_in_flight_as_interrupted():
    for state in (
        LadderState.CHARACTERIZE,
        LadderState.FIT,
        LadderState.PROPOSE,
        LadderState.VERIFY_DEEPEST,
        LadderState.LADDER,
        LadderState.SUSTAIN_TEST,
        LadderState.DERIVE_ANCHORS,
    ):
        retired = transition(_session(state), LadderEvent("restart"))
        assert retired.state == LadderState.ABORTED
        assert retired.disposition == "interrupted"


def test_restart_leaves_settled_states_untouched():
    for state in (LadderState.IDLE, LadderState.REVIEW, LadderState.ABORTED):
        session = _session(state)
        assert transition(session, LadderEvent("restart")) is session


# --------------------------------------------------------------------------- #
# Rung verdicts — each stop-condition, ceiling = previous rung
# --------------------------------------------------------------------------- #


def test_evaluate_rung_clean_pass():
    verdict = evaluate_rung(
        [0.0] * 11, 0.001, mic_moved_flag=False, capture_clipped=False,
        repeat_spread_db=0.0, snr_ok=True, digital_reached=False, margin=NORMAL,
        decision_band_hz=DECISION_BAND,
    )
    assert verdict == RungVerdict(True)


def test_evaluate_rung_quality_precedes_envelope():
    # A clipped, compressed, over-THD rung is reported as the clip (highest precedence).
    verdict = evaluate_rung(
        [-9.0] * 11, 9.0, mic_moved_flag=True, capture_clipped=True,
        repeat_spread_db=9.0, snr_ok=False, digital_reached=True, margin=NORMAL,
        decision_band_hz=DECISION_BAND,
    )
    assert verdict == RungVerdict(False, limited_by="mic_clip")


def _ceiling_for(third_rung: RungMeasurement) -> tuple[tuple[RungRecord, ...], CeilingResult]:
    return run_ladder([_measure(0, 0.0, 30), _measure(1, 3.0, 40), third_rung], NORMAL)


def test_run_ladder_compression_stop_ceiling_is_previous():
    records, ceiling = _ceiling_for(_measure(2, 6.0, 50, fund=_compression_fail_fund(6.0)))
    assert records[2].verdict == RungVerdict(False, limited_by="compression")
    assert ceiling.limited_by == "compression"
    assert ceiling.ceiling_rung_ordinal == 1 and ceiling.listening_level == 40


def test_run_ladder_thd_stop_ceiling_is_previous():
    records, ceiling = _ceiling_for(_measure(2, 6.0, 50, harmonics=_thd_fail_harmonics(6.0)))
    assert records[2].verdict == RungVerdict(False, limited_by="thd")
    assert ceiling.ceiling_rung_ordinal == 1 and ceiling.listening_level == 40


def test_run_ladder_clip_stop_ceiling_is_previous():
    _, ceiling = _ceiling_for(_measure(2, 6.0, 50, capture_clipped=True))
    assert ceiling.limited_by == "mic_clip"
    assert ceiling.ceiling_rung_ordinal == 1 and ceiling.listening_level == 40


def test_run_ladder_repeat_spread_stop_ceiling_is_previous():
    _, ceiling = _ceiling_for(_measure(2, 6.0, 50, repeat_spread_db=3.0))
    assert ceiling.refusal is BassExtensionRefusal.CAPTURE_QUALITY_REFUSED
    assert ceiling.ceiling_rung_ordinal == 1 and ceiling.listening_level == 40


def test_run_ladder_snr_stop_ceiling_is_previous():
    _, ceiling = _ceiling_for(_measure(2, 6.0, 50, snr_ok=False))
    assert ceiling.refusal is BassExtensionRefusal.CAPTURE_SNR_INSUFFICIENT
    assert ceiling.ceiling_rung_ordinal == 1 and ceiling.listening_level == 40


def test_run_ladder_digital_stop_ceiling_is_previous():
    _, ceiling = _ceiling_for(_measure(2, 6.0, 50, digital_reached=True))
    assert ceiling.limited_by == "digital"
    assert ceiling.ceiling_rung_ordinal == 1 and ceiling.listening_level == 40


def test_run_ladder_mic_moved_stop_ceiling_is_previous():
    records, ceiling = _ceiling_for(_measure(2, 6.0, 50, fund=_mic_moved_fund(6.0)))
    assert records[2].verdict.refusal is BassExtensionRefusal.MIC_MOVED_BETWEEN_RUNGS
    assert ceiling.refusal is BassExtensionRefusal.MIC_MOVED_BETWEEN_RUNGS
    assert ceiling.ceiling_rung_ordinal == 1 and ceiling.listening_level == 40


def test_run_ladder_first_rung_failure_has_no_clean_ceiling():
    _, ceiling = run_ladder([_measure(0, 0.0, 30, capture_clipped=True)], NORMAL)
    assert ceiling.listening_level is None and ceiling.ceiling_rung_ordinal is None
    assert ceiling.limited_by == "mic_clip"


def test_run_ladder_all_pass_is_incomplete():
    _, ceiling = run_ladder([_measure(0, 0.0, 30), _measure(1, 3.0, 40)], NORMAL)
    assert ceiling.refusal is BassExtensionRefusal.LADDER_INCOMPLETE


def test_run_ladder_rejects_empty_series():
    with pytest.raises(LadderError):
        run_ladder([], NORMAL)


# --------------------------------------------------------------------------- #
# Sustain
# --------------------------------------------------------------------------- #


def _laddered_ceiling():
    records, ceiling = _ceiling_for(_measure(2, 6.0, 50, harmonics=_thd_fail_harmonics(6.0)))
    return records, ceiling


def test_sustain_pass_records_evidence():
    result = sustain_result([0.0, 0.0], [0.2, 0.1], 60.0, 61.0, NORMAL)
    assert result.verdict == "passed" and result.limited_by is None
    assert result.duration_s == NORMAL.sustain_duration_s
    records, ceiling = _laddered_ceiling()
    assert apply_sustain(records, ceiling, result) is ceiling


def test_sustain_sag_lowers_one_rung():
    result = sustain_result([0.0, 0.0], [-2.0, 0.0], 60.0, 60.0, NORMAL)
    assert result.verdict == "lowered" and result.limited_by == "sustain_sag"
    records, ceiling = _laddered_ceiling()
    lowered = apply_sustain(records, ceiling, result)
    assert lowered.ceiling_rung_ordinal == 0 and lowered.listening_level == 30
    assert lowered.limited_by == "sustain_sag"


def test_sustain_fc_shift_lowers_one_rung():
    result = sustain_result([0.0], [0.0], 60.0, 66.0, NORMAL)
    assert result.verdict == "lowered" and result.limited_by == "sustain_fc_shift"
    records, ceiling = _laddered_ceiling()
    lowered = apply_sustain(records, ceiling, result)
    assert lowered.ceiling_rung_ordinal == 0 and lowered.limited_by == "sustain_fc_shift"


def test_sustain_rejects_mismatched_vectors():
    with pytest.raises(LadderError):
        sustain_result([0.0, 0.0], [0.0], 60.0, 60.0, NORMAL)


# --------------------------------------------------------------------------- #
# Anchor derivation
# --------------------------------------------------------------------------- #


def _family():
    return (
        TargetSpec("t20", 20.0, 0.65, (), 12.0, None),
        TargetSpec("t28", 28.0, 0.65, (), 8.0, None),
        TargetSpec("t40", 40.0, 0.65, (), 4.0, None),
        TargetSpec("natural", 60.0, 0.72, (), 0.0, None),
    )


def test_derive_anchor_set_interpolates_and_tags_evidence():
    family = _family()
    measured = (
        AnchorPoint("t20", 40, "measured"),
        AnchorPoint("t40", 70, "spot_verified"),
    )
    anchors = derive_anchor_set(family, measured, NORMAL)
    by_id = {a.target_id: a for a in anchors}
    assert set(by_id) == {"t20", "t28", "t40"}
    assert by_id["t20"].evidence == "measured"
    assert by_id["t40"].evidence == "spot_verified"
    assert by_id["t28"].evidence == "derived"
    assert all(0 <= a.max_listening_level <= 100 for a in anchors)


# --------------------------------------------------------------------------- #
# Manifest validation + silent preflight
# --------------------------------------------------------------------------- #

_MANIFEST = {
    "margin": "normal",
    "enclosure_kind": "sealed",
    "cabinet": {
        "enclosure_kind": "sealed",
        "radiator_count": 1,
        "effective_radiating_diameter_mm": 120.0,
        "baffle_width_mm": 180.0,
    },
}


def test_validate_manifest_accepts_and_resolves_adapter():
    manifest = validate_commissioning_manifest(_MANIFEST)
    assert isinstance(manifest, CommissioningManifest)
    assert manifest.adapter_id == "sealed_v1" and manifest.n_targets == 5


@pytest.mark.parametrize(
    "mutation",
    [
        {"margin": "reckless"},
        {"enclosure_kind": "quantum_horn"},
        {"cabinet": "not-a-mapping"},
        {"n_targets": 1},
        {"surprise": True},
        {"mic_calibration_id": 123},
        {"mic_calibration_id": ""},
    ],
)
def test_validate_manifest_refuses_malformed(mutation):
    bad = {**_MANIFEST, **mutation}
    with pytest.raises(LadderManifestError):
        validate_commissioning_manifest(bad)


def test_preflight_ok_and_silent(capsys):
    result = preflight(
        {
            "baseline_applied": True,
            "driver_safety_current": True,
            "measurement_window_available": True,
            "capture_reachable": True,
            "mic_calibrated": True,
        }
    )
    assert result.ok and result.blocking == () and result.warnings == ()
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_preflight_blocks_and_warns():
    result = preflight({"measurement_window_available": True, "capture_reachable": True})
    assert not result.ok
    assert BassExtensionRefusal.BASELINE_NOT_APPLIED.value in result.blocking
    assert "driver_safety_not_current" in result.blocking
    assert result.warnings == ("mic_uncalibrated",)


# --------------------------------------------------------------------------- #
# Fully injected synthetic dry run
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    adapter_id = "sealed_v1"
    adapter_version = 1
    required_captures = (CaptureRole.WOOFER_NEARFIELD,)

    def __init__(self, plant, family):
        self._plant = plant
        self._family = family

    def fit_plant(self, captures, cabinet):
        return self._plant

    def generate_family(self, plant, *, margin, n_targets=5):
        return self._family

    def predicted_response(self, plant, target, freqs_hz):
        return np.zeros_like(np.asarray(freqs_hz, dtype=np.float64))


def _dry_run_inputs(**overrides) -> DryRunInputs:
    family = _family()
    base = dict(
        manifest=_MANIFEST,
        preconditions={
            "baseline_applied": True,
            "driver_safety_current": True,
            "measurement_window_available": True,
            "capture_reachable": True,
            "mic_calibrated": True,
        },
        adapter=_FakeAdapter(plant=object(), family=family),
        capture_curves={
            CaptureRole.WOOFER_NEARFIELD: MagnitudeCurve(tuple(FREQS), tuple(BASE))
        },
        capture_records=(CaptureRecord(CaptureRole.WOOFER_NEARFIELD, (_aid("cap"),), "pass"),),
        rung_measurements=(
            _measure(0, 0.0, 30),
            _measure(1, 3.0, 40),
            _measure(2, 6.0, 50, harmonics=_thd_fail_harmonics(6.0)),
        ),
        spot_anchors=(
            AnchorPoint("t28", 55, "spot_verified"),
            AnchorPoint("t40", 70, "spot_verified"),
        ),
        sustain_pre_band_levels=(0.0, 0.0, 0.0),
        sustain_post_band_levels=(0.0, 0.0, 0.0),
        sustain_fc_pre_hz=60.0,
        sustain_fc_post_hz=60.0,
    )
    base.update(overrides)
    return DryRunInputs(**base)


def test_synthetic_dry_run_reaches_review():
    session = synthetic_dry_run(_dry_run_inputs())
    assert session.state == LadderState.REVIEW
    assert session.disposition is None
    assert len(session.family) == 4
    assert session.ceiling.listening_level == 40
    assert session.sustain.verdict == "passed"
    assert {a.target_id for a in session.anchors} == {"t20", "t28", "t40"}


def test_synthetic_dry_run_publishes_no_limiter_threshold():
    session = synthetic_dry_run(_dry_run_inputs())
    assert all(target.limiter_threshold_dbfs is None for target in session.family)


def test_synthetic_dry_run_aborts_on_preflight_block():
    session = synthetic_dry_run(_dry_run_inputs(preconditions={"capture_reachable": True}))
    assert session.state == LadderState.ABORTED
    assert BassExtensionRefusal.BASELINE_NOT_APPLIED.value in session.refusals


def test_synthetic_dry_run_aborts_on_fit_refusal():
    refusal = FitRefusal("bass_extension_sealed_fit_untrustworthy", "synthetic fit refusal")
    session = synthetic_dry_run(
        _dry_run_inputs(adapter=_FakeAdapter(plant=refusal, family=_family()))
    )
    assert session.state == LadderState.ABORTED
    assert session.disposition == "fit_refused"
    assert session.refusals == ("bass_extension_sealed_fit_untrustworthy",)


def test_synthetic_dry_run_aborts_on_ladder_incomplete():
    # First rung clips -> no clean ceiling -> the ladder-incomplete abort arm.
    session = synthetic_dry_run(
        _dry_run_inputs(rung_measurements=(_measure(0, 0.0, 30, capture_clipped=True),))
    )
    assert session.state == LadderState.ABORTED
    assert session.refusals == (BassExtensionRefusal.LADDER_INCOMPLETE.value,)


# --------------------------------------------------------------------------- #
# Synthetic limiter-evidence intake (typed result consumed in memory)
# --------------------------------------------------------------------------- #


def test_synthetic_limiter_intake_threshold_set():
    context = _LEV._context("deep", "natural")
    bundle = _LEV._evidence(
        context,
        [_LEV._target("deep", _LEV.DEEP_THRESHOLD), _LEV._target("natural", _LEV.NATURAL_THRESHOLD)],
    )
    result = synthetic_limiter_intake(bundle, context)
    assert isinstance(result, LimiterThresholdSet)
    assert len(result.targets) == 2


def test_synthetic_limiter_intake_refusal():
    result = synthetic_limiter_intake({"garbage": 1}, {"garbage": 1})
    assert isinstance(result, LimiterEvidenceRefusal)


# --------------------------------------------------------------------------- #
# Scope guards (mirror the repo AST guard within this slice)
# --------------------------------------------------------------------------- #

_LADDER_SOURCE = Path(L.__file__).read_text(encoding="utf-8")
_LADDER_TREE = ast.parse(_LADDER_SOURCE)


def test_no_wave3_transaction_references():
    for name in ("apply_bass_extension", "bypass_bass_extension", "recover_pending_bass_extension_apply"):
        assert name not in _LADDER_SOURCE


def test_producer_import_is_function_local_only():
    module_imports = [
        alias.name
        for node in _LADDER_TREE.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert "produce_limiter_thresholds" not in module_imports
    local_imports = [
        alias.name
        for node in ast.walk(_LADDER_TREE)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if node not in _LADDER_TREE.body
    ]
    assert "produce_limiter_thresholds" in local_imports


def test_start_session_binds_manifest():
    manifest = validate_commissioning_manifest(_MANIFEST)
    session = start_session("bex-1", manifest)
    assert session.state == LadderState.IDLE
    assert session.margin == "normal" and session.adapter_id == "sealed_v1"
