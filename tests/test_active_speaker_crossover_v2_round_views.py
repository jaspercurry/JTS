# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The round-grading comparison views. Four ported from a laptop campaign
(issue #2769): frozen-reference grading, per-seat curves including the
VERIFY pose, session-to-session repeatability, and per-seat agreement. A
fifth, audibility-weighted co-metrics (NBD/SM, Olive 2004), landed with
ticket 6.13 / ADR-0202.

Every fixture builds its ``cloud_verify.json`` ``spec`` block by calling the
REAL :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` on a
synthetic combined curve and persisting its own ``to_dict()`` — the same
shape a real banked round carries — rather than hand-typing a partial dict,
so a schema drift in :class:`~jasper.active_speaker.flat_spec.FlatSpecReport`
fails this suite instead of silently going unnoticed.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.forward_model import (
    ACCEPTANCE_NOT_RUN,
    SummationCandidate,
)
from jasper.active_speaker.crossover_v2 import round_inputs as round_inputs_mod
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.round_views import (
    ENTRY_STATE_UNREADABLE,
    RoundViewsError,
    agreement_table,
    audibility_co_metrics,
    entry_state_grade,
    forward_model_verify_delta,
    frozen_reference_grade,
    BankedRound,
    NOT_SWEPT_BAND_NOT_EVALUABLE,
    NOT_SWEPT_CAPTURES_UNREADABLE,
    NOT_SWEPT_SINGLE_POSE,
    load_banked_round,
    per_seat_curves,
    pooled_window_horizontal,
    repeatability_spread,
    spec_with_gate_sensitivity,
    verify_pose_curve,
)
from jasper.active_speaker.crossover_v2.gate_sweep import ROUTE_SIGMA_GROWTH, WINDOW_MOVED
from jasper.active_speaker.crossover_v2.round_captures import REFUSE_NO_CAPTURES
from jasper.active_speaker import flat_spec
from jasper.active_speaker.flat_spec import evaluate_flat_spec

from tests.crossover_v2_banked_round import (
    MODE_TWO_WAY,
    MODE_WAY1,
    SOLO_BAND_HZ,
    bank_measure_round,
    bank_verify_round,
)
from tests.crossover_v2_fixtures import bank_capture_round
# The gate sweep's own pose IRs, reused rather than copied, so a deconvolved
# round's answer is as knowable here as it is there.
from tests.test_crossover_v2_gate_sweep import FEATURE_HZ, _pose_ir

#: A live session bundle resolves its three non-bundle inputs to the on-speaker
#: SSOT paths; no test may read whatever sits at those absolute paths on the
#: box running pytest.
pytestmark = pytest.mark.usefixtures("no_real_pi_paths")

#: A log-spaced curve grid spanning all three SPEC_BANDS rows
#: (250-2000 / 2000-8000 / 8000-16000 Hz) with plenty of bins in each.
GRID = np.geomspace(280.0, 16000.0, 90)
REFERENCE_DB = -20.0


def _flat_curve(*, offset_db: float = 0.0, ripple_db: float = 0.0) -> np.ndarray:
    """A curve at ``REFERENCE_DB + offset_db``, with optional deterministic
    ripple (a single +ripple_db bump at bin 10, -ripple_db at bin 40) so a
    test can tell a perfectly-flat golden case apart from a rippled one."""
    curve = np.full(GRID.shape, REFERENCE_DB + offset_db, dtype=float)
    if ripple_db:
        curve[10] += ripple_db
        curve[40] -= ripple_db
    return curve


def _spec_dict(
    combined_db: np.ndarray, *, smoothing_fraction: int = 12, trusted_floor_hz: float | None = None
) -> dict[str, Any]:
    """A real ``FlatSpecReport.to_dict()`` for ``combined_db`` on :data:`GRID`."""
    mask = np.zeros(GRID.shape, dtype=bool)
    report = evaluate_flat_spec(
        GRID, combined_db, mask,
        smoothing_fraction=smoothing_fraction, trusted_floor_hz=trusted_floor_hz,
    )
    return report.to_dict()


def _make_round_dir(
    tmp_path: Path,
    name: str,
    *,
    position_curves: dict[str, tuple[str, np.ndarray]],
    combined_db: np.ndarray | None = None,
    spec_smoothing_fraction: int = 12,
    positions_smoothing_fraction: int | None = None,
    trusted_floor_hz: float | None = None,
    position_degrees: dict[str, float] | None = None,
) -> Path:
    """One banked round directory, in the tree ``bank-crossover-round.sh``
    produces: ``<round-dir>/bundle/<session>/evidence/v1/artifacts/crossover_v2/<capture>/``.

    ``position_curves`` maps ``position_id -> (role, magnitude_db)``.
    ``spec_smoothing_fraction`` / ``positions_smoothing_fraction`` are
    separate on purpose (they default equal): a real banked round's
    combined-curve ``spec`` block and its per-position ``curve_grid`` block
    are not always smoothed at the same fraction, and B3's mismatched-
    fraction test needs to set them apart deliberately.
    """
    if positions_smoothing_fraction is None:
        positions_smoothing_fraction = spec_smoothing_fraction
    round_dir = tmp_path / name
    session_dir = round_dir / "bundle" / "sess1"
    capture_dir = session_dir / "evidence/v1/artifacts/crossover_v2" / "cap1"
    capture_dir.mkdir(parents=True)

    (session_dir / "info.json").write_text(json.dumps({
        "kind": "jts_active_speaker_commissioning_bundle",
        "session_id": "sess1", "state": "closed", "started_at": 1.0,
        "placement": {"policy_id": "driver_same_distance_v1", "acknowledged": True},
        "fingerprints": {
            "topology_id": "default", "topology_fingerprint": "abc123",
            "output_assignments": [],
            "graph_fingerprint": None,
            "mic": {"calibration_id": "", "calibration_sha256": None},
            "build_sha": "deadbeef",
        },
    }))
    (capture_dir / "round_receipt.json").write_text(json.dumps({
        "kind": "jts_crossover_v2_round_receipt", "schema_version": 2, "round_id": "r1",
    }))
    if combined_db is None:
        # Power-mean the supplied positions when the caller doesn't care.
        stack = np.vstack([curve for _role, curve in position_curves.values()])
        combined_db = 10.0 * np.log10(np.mean(10.0 ** (stack / 10.0), axis=0))
    # ``position_deg`` is present only for the seats the caller named, exactly
    # as the real writer behaves: the packet's row filter drops a key whose
    # value is None, so a seat with no commanded bearing — and every seat of a
    # round banked before the 2026-08-24 geometry writer — carries no key at
    # all rather than a null.
    degrees = position_degrees or {}
    positions = [
        {
            "position_id": position_id, "index": index, "attempt": 1,
            "role": role, "take_id": "", "magnitude_db": curve.tolist(),
            **({"position_deg": degrees[position_id]} if position_id in degrees else {}),
        }
        for index, (position_id, (role, curve)) in enumerate(position_curves.items(), start=2)
    ]
    cloud = {
        "kind": "jts_crossover_v2_cloud_evidence", "schema_version": 1,
        "trusted_floor_hz": trusted_floor_hz, "validity_floor_hz": None,
        "curve": {"freqs_hz": GRID.tolist(), "magnitude_db": combined_db.tolist()},
        "flatness": {"evaluable": True, "n_bins": len(GRID), "n_excluded": 0, "rms_db": 0.0},
        "spec": _spec_dict(
            combined_db, smoothing_fraction=spec_smoothing_fraction, trusted_floor_hz=trusted_floor_hz,
        ),
        "merged_excluded_bands_hz": [], "screen_excluded_bands_hz": [],
        "null_registry": {"classification": "insufficient_evidence", "nulls": []},
        "null_registry_crossover_region": {"classification": "insufficient_evidence"},
        "carve_outs": [], "geometry": {"reason": "thin_evidence", "n_positions": len(positions)},
        "positions": {
            "available": True, "schema": "jts_attribution_position_evidence/1",
            "curve_grid": {
                "freqs_hz": GRID.tolist(), "fractional_octave": positions_smoothing_fraction,
                "smoothing_fraction": positions_smoothing_fraction, "floor_hz": None, "floor_source": None,
            },
            "positions": positions,
        },
    }
    (capture_dir / "cloud_verify.json").write_text(json.dumps(cloud))
    (capture_dir / "findings_cloud_verify.json").write_text(json.dumps({
        "findings": [], "field_descriptions": {},
    }))
    return round_dir


# --------------------------------------------------------------------------- #
# load_banked_round
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("live", [False, True])
def test_a_round_loads_from_its_banked_tree_or_from_the_live_bundle(
    tmp_path, live
):
    """The SAME round, read the two ways it can be pointed at (#3498, #2882).

    A banked tree is the live bundle one level deeper plus three frozen
    siblings, so both readings must produce the same views — and the one thing
    that cannot be re-derived from the paths, which of the two shapes was
    found, is disclosed rather than inferred.
    """
    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={
            "cloud_verify_02": ("onax", _flat_curve()),
            "cloud_verify_04": ("offax", _flat_curve()),
        },
    )
    session_dir = round_dir / "bundle" / "sess1"

    loaded = load_banked_round(session_dir if live else round_dir)

    assert loaded.inputs.banked is not live
    assert loaded.inputs.session_dir == session_dir
    assert loaded.session_dir == session_dir
    assert {p.position_id for p in loaded.positions} == {
        "cloud_verify_02", "cloud_verify_04"
    }
    assert loaded.curve_grid_hz.shape == GRID.shape
    assert loaded.graded_report.bands  # a real, non-empty rehydrated report


def test_a_live_bundle_takes_the_flow_state_only_when_it_names_that_session(
    tmp_path, monkeypatch
):
    """One flow state on the speaker, a dozen retained session directories.

    Every live bundle resolves to the SAME state file, so an older retained
    session handed the current one would be graded on another round's verify
    curve, verdicts and ordinal — wrong numbers rather than missing ones. The
    two ids are compared in the namespace they share: the state's own
    ``session_id`` against the capture directory the bundle filed its round
    artifacts under.
    """
    curves = {"cloud_verify_02": ("onax", _flat_curve())}
    mine = _make_round_dir(tmp_path, "r1", position_curves=curves) / "bundle" / "sess1"
    other = _make_round_dir(tmp_path, "r2", position_curves=curves) / "bundle" / "sess1"
    capture = other / "evidence/v1/artifacts/crossover_v2"
    (capture / "cap1").rename(capture / "cap2")
    state = tmp_path / "flow-state.json"
    state.write_text(json.dumps({"session_id": "cap2"}))
    monkeypatch.setattr(round_inputs_mod, "STATE_DEFAULT_PATH", state)

    assert round_inputs_mod.round_inputs(other).state_path == state
    stale = round_inputs_mod.round_inputs(mine)
    assert stale.state_path is None
    assert stale.state_reason == round_inputs_mod.STATE_SESSION_UNKNOWN


def test_a_directory_of_neither_shape_refuses(tmp_path):
    """No ``bundle/`` and no ``info.json`` is neither round shape, and the
    refusal keeps this module's own type so the CLI's load stage catches it."""
    neither = tmp_path / "neither"
    neither.mkdir()
    with pytest.raises(RoundViewsError):
        load_banked_round(neither)


def test_load_banked_round_refuses_multiple_bundle_sessions(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    (round_dir / "bundle" / "second_session").mkdir()
    with pytest.raises(RoundViewsError, match="expected exactly one"):
        load_banked_round(round_dir)


def test_a_cloud_group_whose_every_row_lost_its_curve_is_named_as_that(tmp_path):
    """A TRUNCATED packet and a round that banked no cloud group are two
    different absences, and only one of them is a round shape.

    The seat rows are there, the block says ``available``, and not one row
    carries a ``magnitude_db``: nothing measured that is readable, which is a
    corrupt or half-written packet. Told apart from the stage-1 shape below
    because the two send an operator to different places — one to the bank,
    one to the stage they asked for — and the shape sentence over a corrupt
    packet reads as "this round is fine, you asked the wrong view of it".
    """
    corrupt = load_banked_round(_make_round_dir(
        tmp_path, "r1",
        position_curves={"cloud_verify_02": ("onax", np.asarray([]))},
        combined_db=_flat_curve(),
    ))
    with pytest.raises(RoundViewsError, match="every position row is missing its magnitude_db"):
        corrupt.graded_positions

    # The control: a round that banked no cloud group at all keeps the shape
    # sentence, which is what #3478 made it mean.
    stage_one = load_banked_round(bank_measure_round(tmp_path))
    with pytest.raises(RoundViewsError, match="carries no position evidence"):
        stage_one.graded_positions


# --------------------------------------------------------------------------- #
# View 1 — frozen_reference_grade
# --------------------------------------------------------------------------- #


def test_frozen_equals_shipped_when_target_is_the_baseline(tmp_path):
    """Grading a round against itself: the freeze changes nothing."""
    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={
            "cloud_verify_02": ("onax", _flat_curve(ripple_db=1.0)),
            "cloud_verify_04": ("offax", _flat_curve(ripple_db=0.5)),
        },
    )
    banked = load_banked_round(round_dir)
    result = frozen_reference_grade(banked, banked)
    assert result.shipped == pytest.approx(result.frozen, abs=1e-9)


def test_frozen_reference_recovers_an_injected_level_shift_exactly(tmp_path):
    """The golden case the module docstring derives: a perfectly FLAT
    baseline and a perfectly flat target shifted by ``delta_db`` grade
    SHIPPED as zero-residual either way (a pure level shift is invisible to
    a curve's own reference — power-mean is exactly additive under a
    per-bin constant dB shift), but FROZEN grades the target against the
    baseline's un-shifted reference, so every bin's frozen deviation is
    exactly ``delta_db`` and the pooled RMS is exactly ``abs(delta_db)``.
    """
    delta_db = 0.6
    baseline_dir = _make_round_dir(
        tmp_path, "baseline",
        position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    target_dir = _make_round_dir(
        tmp_path, "target",
        position_curves={"cloud_verify_02": ("onax", _flat_curve(offset_db=delta_db))},
    )
    baseline = load_banked_round(baseline_dir)
    target = load_banked_round(target_dir)
    result = frozen_reference_grade(baseline, target)

    assert result.shipped["onax"] == pytest.approx(0.0, abs=1e-9)
    assert result.frozen["onax"] == pytest.approx(abs(delta_db), abs=1e-6)
    assert result.shipped_positions["cloud_verify_02"] == pytest.approx(0.0, abs=1e-9)
    assert result.frozen_positions["cloud_verify_02"] == pytest.approx(abs(delta_db), abs=1e-6)


def test_frozen_reference_grader_matches_evaluate_flat_spec_directly(tmp_path):
    """The SHIPPED half is not a re-derivation: it must equal what calling
    the real evaluator directly on the same curve produces."""
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve(ripple_db=0.8))},
    )
    banked = load_banked_round(round_dir)
    result = frozen_reference_grade(banked, banked)

    direct = evaluate_flat_spec(
        GRID, _flat_curve(ripple_db=0.8), np.zeros(GRID.shape, dtype=bool),
        smoothing_fraction=12, trusted_floor_hz=None,
    )
    from jasper.active_speaker.flat_spec import spec_convergence_residual
    expected = spec_convergence_residual(direct)
    assert result.shipped["onax"] == pytest.approx(expected.rms_db, abs=1e-9)


def test_frozen_reference_refuses_a_target_position_absent_from_baseline(tmp_path):
    baseline_dir = _make_round_dir(
        tmp_path, "baseline", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    target_dir = _make_round_dir(
        tmp_path, "target", position_curves={"cloud_verify_09": ("onax", _flat_curve())},
    )
    baseline = load_banked_round(baseline_dir)
    target = load_banked_round(target_dir)
    with pytest.raises(RoundViewsError, match="no baseline counterpart"):
        frozen_reference_grade(baseline, target)


# --------------------------------------------------------------------------- #
# View 2 — verify_pose_curve + per_seat_curves
# --------------------------------------------------------------------------- #


def _bank_verify_measured(
    round_dir: Path,
    *,
    freqs_hz: np.ndarray | None = None,
    measured_db: np.ndarray | None = None,
) -> Path:
    """Bank a ``state.json`` carrying ``verify_priors.verify_measured``.

    The record is built by the PRODUCT's own persist-side reducer rather than
    hand-typed, for this suite's standing reason: a drift in what
    ``persist_conductor_state`` writes must fail here instead of leaving the
    fixture agreeing with nothing that ships.
    """
    from jasper.active_speaker.crossover_v2.durable_state import (
        _decimate_verify_measured,
    )

    freqs_hz = GRID if freqs_hz is None else freqs_hz
    measured_db = _flat_curve() if measured_db is None else measured_db
    record = _decimate_verify_measured(
        (freqs_hz, measured_db, np.zeros_like(np.asarray(measured_db, dtype=float)))
    )
    path = round_dir / "state.json"
    path.write_text(json.dumps({"verify_priors": {"verify_measured": record}}))
    return path


def test_verify_pose_curve_reads_the_banked_curve_onto_the_rounds_grid(tmp_path):
    """The VERIFY curve is READ from the bank, and made comparable in one hop.

    Banked on a DIFFERENT, coarser grid than the round's on purpose: the
    persisted curve sits on the VERIFY capture's own frequencies, so an
    implementation that handed the banked array straight back could not pass
    this. The two endpoints are shared between the grids, so they pin the
    VALUES as banked — no re-derivation and no re-levelling — while the
    whole-array check pins the interpolation rule that carries the rest.
    """
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    banked_freqs = np.geomspace(GRID[0], GRID[-1], 41)
    banked_db = REFERENCE_DB + 3.0 * np.log2(banked_freqs / banked_freqs[0])
    _bank_verify_measured(round_dir, freqs_hz=banked_freqs, measured_db=banked_db)

    result = verify_pose_curve(load_banked_round(round_dir))

    assert result.reason == ""
    assert result.curve is not None
    assert np.array_equal(result.curve.freqs_hz, GRID)
    assert result.curve.magnitude_db[0] == pytest.approx(float(banked_db[0]))
    assert result.curve.magnitude_db[-1] == pytest.approx(float(banked_db[-1]))
    assert np.allclose(
        result.curve.magnitude_db, np.interp(GRID, banked_freqs, banked_db)
    )
    # The persisted curve is block-averaged in dB, never smoothed at a
    # fractional-octave width, so the attestation is "not attested" rather
    # than a fraction this reader would have had to invent.
    assert result.curve.smoothing_fraction == 0
    # What the phase MEANS, not an angle recovered from a walk log.
    assert result.curve.degrees == 0.0


@pytest.mark.parametrize(
    "written",
    [
        None,
        "{not json",
        "[]",
        '{"verify_priors": {}}',
        '{"verify_priors": {"verify_measured": {}}}',
    ],
    ids=["no_state_file", "unreadable", "not_an_object", "no_key", "empty_record"],
)
def test_verify_pose_curve_names_why_it_has_no_curve(tmp_path, written):
    """Every absence answers the same shape: no curve, WITH a reason.

    A round banked before the curve was persisted, one banked without its
    state file, and one whose state file is damaged are all "there is no
    VERIFY curve to compare" — and none of them may raise, because the three
    other views of the same round are still perfectly readable.
    """
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    if written is not None:
        (round_dir / "state.json").write_text(written)

    result = verify_pose_curve(load_banked_round(round_dir))

    assert result.curve is None
    assert result.reason


def test_forward_model_verify_delta_joins_the_two_rounds_the_flow_banks(
    tmp_path,
):
    """The prediction basis and the measured VERIFY sum come from DIFFERENT
    banked rounds, because that is where the flow puts them (#3482).

    Stage 1 banks the per-driver solos and no VERIFY; stage 2 banks the VERIFY
    and no solos — ``jasper.web.correction_crossover_v2`` opens a new bundle
    for stage 2 — so a comparison that read one round for both halves could
    never run on a banked corpus. Both fixture rounds come from the shared
    real-shape builder, so this pin fails if either stage's writer moves.
    """
    basis = bank_measure_round(tmp_path)
    measured = bank_verify_round(tmp_path)

    result = forward_model_verify_delta(
        load_banked_round(basis), SummationCandidate(),
        measured=load_banked_round(measured),
    )

    assert result.reason == ""
    assert result.delta is not None
    assert result.delta["compared_points"] > 0
    assert result.delta["take_path"].endswith("positions/measure_02_a01.json")
    # WHICH two rounds were joined, on the result rather than left for a reader
    # to remember: a delta whose halves came from different rounds and does not
    # say so is a number with no provenance.
    assert result.basis_round_dir == str(basis)
    assert result.measured_round_dir == str(measured)


@pytest.mark.parametrize(
    ("bank_measured", "bank_basis", "basis_mode"),
    [
        pytest.param(False, True, MODE_TWO_WAY, id="no_measured_verify_sum"),
        pytest.param(True, False, MODE_TWO_WAY, id="no_prediction_basis"),
        # A subless passive main banks ONE solo: the forward model is the one
        # view here that is about a pair, and a legal speaker shape must reach
        # the same named refusal rather than raising or inventing a branch.
        pytest.param(True, True, MODE_WAY1, id="a_way1_basis_has_no_pair"),
    ],
)
def test_forward_model_verify_delta_names_the_half_it_was_not_given(
    tmp_path, bank_measured, bank_basis, basis_mode,
):
    """Either half absent answers the same shape as every other view here: no
    delta, WITH a reason, and never a raise.

    The absent half is a REAL absence in each case — a stage-1 round banks no
    VERIFY curve, a stage-2 round banks no solos, a 1-way round banks no pair —
    so each parameter is the refusal an operator actually meets rather than a
    mutilated fixture.
    """
    basis = (
        bank_measure_round(tmp_path, mode=basis_mode)
        if bank_basis else bank_verify_round(tmp_path)
    )
    measured = (
        bank_verify_round(tmp_path, name="measured")
        if bank_measured else bank_measure_round(tmp_path, name="measured")
    )

    result = forward_model_verify_delta(
        load_banked_round(basis), SummationCandidate(),
        measured=load_banked_round(measured),
    )

    assert result.delta is None
    assert result.reason
    # A result with no delta was judged by no measurement, and says so rather
    # than leaving the acceptance question to whoever reads it later (#3481).
    assert result.acceptance["status"] == ACCEPTANCE_NOT_RUN
    assert result.acceptance["judged_against"] is None


def test_forward_model_verify_delta_reads_the_verify_curve_off_its_own_grid(
    tmp_path,
):
    """The banked VERIFY curve reaches the comparison VERBATIM.

    It used to be resampled onto the round's cloud-position grid first, which
    a round that banked no cloud group does not have — and the delta then
    compared an empty curve. The measured curve here is flat, the solos sum to
    a flat prediction, so the whole difference is LEVEL: a shape delta of zero
    over the solos' own swept band is the tell that the real curve arrived.
    """
    basis = bank_measure_round(tmp_path)
    measured = bank_verify_round(tmp_path)

    result = forward_model_verify_delta(
        load_banked_round(basis), SummationCandidate(),
        measured=load_banked_round(measured),
    )

    assert result.delta is not None
    assert result.delta["max_abs_db"] == pytest.approx(0.0, abs=1e-6)
    assert result.delta["compared_band_hz"] == [SOLO_BAND_HZ[0], SOLO_BAND_HZ[1]]


def test_per_seat_curves_includes_every_position_and_the_verify_pose(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={
            "cloud_verify_02": ("onax", _flat_curve(offset_db=2.0)),
            "cloud_verify_04": ("offax", _flat_curve(offset_db=-3.0)),
        },
    )
    _bank_verify_measured(round_dir, measured_db=_flat_curve(offset_db=1.0))
    banked = load_banked_round(round_dir)
    verify = verify_pose_curve(banked)

    seats = per_seat_curves(banked, verify.curve)
    assert {s.position_id for s in seats} == {"cloud_verify_02", "cloud_verify_04", "verify"}
    # Every seat is self-normalised: a constant offset added before
    # normalisation must vanish from its own normalized curve.
    for seat in seats:
        if seat.position_id == "cloud_verify_02":
            assert np.allclose(seat.normalized_db, 0.0, atol=1e-9)


def test_per_seat_curves_refuses_an_empty_norm_band(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    banked = load_banked_round(round_dir)
    with pytest.raises(RoundViewsError, match="norm band"):
        per_seat_curves(banked, None, norm_band_hz=(50000.0, 60000.0))


def test_per_seat_curves_normalises_by_median_not_mean(tmp_path):
    """Pins the MEDIAN normalisation specifically: a single huge outlier
    bin inside the norm band pulls the MEAN of that band well away from
    the baseline level, but the median (66 bins, one outlier) is untouched.
    A mutation that swapped ``np.median`` for ``np.mean`` would shift every
    baseline bin's ``normalized_db`` off zero by the outlier's pull —
    ~1.06 dB here, far outside float tolerance.
    """
    curve = _flat_curve()
    # GRID spans [280, 16000] Hz log-spaced; the default norm band is
    # [400, 8000] Hz. Push ONE bin inside that band to a large positive
    # outlier, leaving the rest of the band (and the whole curve) untouched.
    sel = (GRID >= 400.0) & (GRID <= 8000.0)
    outlier_idx = int(np.where(sel)[0][0])
    curve[outlier_idx] = 50.0
    assert np.median(curve[sel]) == pytest.approx(REFERENCE_DB)
    assert np.mean(curve[sel]) != pytest.approx(REFERENCE_DB, abs=0.5)

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", curve)},
    )
    banked = load_banked_round(round_dir)
    seats = per_seat_curves(banked, None)
    seat = seats[0]
    # A baseline bin OUTSIDE the outlier and inside the norm band must
    # normalise to exactly zero under median normalisation.
    baseline_idx = int(np.where(sel)[0][1])
    assert seat.normalized_db[baseline_idx] == pytest.approx(0.0, abs=1e-9)


def test_load_banked_round_reads_a_repeat_floor_banked_beside_it(tmp_path):
    """The side file reaches the packet exactly as applied-profile.json does:
    present, the accuracy budget's repeat-floor component is available."""
    from jasper.active_speaker.crossover_v2.round_views import repeat_floor_provenance
    from jasper.active_speaker.repeat_floor import derive_repeat_floor, write_repeat_floor

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    component = "in_capture_repeat_floor"
    absent = load_banked_round(round_dir)
    assert absent.packet["accuracy_budget"]["components"][component]["available"] is False

    # The record the REAL deriver banks from two repeats, never a hand-typed one.
    twin = _make_round_dir(
        tmp_path, "r2", position_curves={"cloud_verify_02": ("onax", _flat_curve(ripple_db=0.2))},
    )
    rounds = [(str(path), load_banked_round(path)) for path in (round_dir, twin)]
    write_repeat_floor(
        derive_repeat_floor(
            repeatability_spread(rounds),
            rounds=[repeat_floor_provenance(label, banked) for label, banked in rounds],
        ),
        state_path=round_dir / "repeat-floor.json",
    )
    present = load_banked_round(round_dir)
    assert present.packet["accuracy_budget"]["components"][component]["available"] is True


# --------------------------------------------------------------------------- #
# View 3 — repeatability_spread
# --------------------------------------------------------------------------- #


def test_repeatability_spread_reports_zero_for_identical_repeats(tmp_path):
    curves = {"cloud_verify_02": ("onax", _flat_curve(ripple_db=0.7))}
    r1 = load_banked_round(_make_round_dir(tmp_path, "r1", position_curves=curves))
    r2 = load_banked_round(_make_round_dir(tmp_path, "r2", position_curves=curves))
    result = repeatability_spread([("session-1", r1), ("session-2", r2)])

    shipped = next(m for m in result.metrics if m.name == "shipped_linear_pool_db")
    assert shipped.values["session-1"] == pytest.approx(shipped.values["session-2"], abs=1e-9)
    spread = shipped.spread()
    assert spread is not None
    assert spread["range"] == pytest.approx(0.0, abs=1e-9)
    assert spread["sd"] == pytest.approx(0.0, abs=1e-9)

    position = next(m for m in result.per_position if m.name == "cloud_verify_02")
    assert position.values["session-1"] == pytest.approx(position.values["session-2"], abs=1e-9)


def test_repeatability_spread_pairwise_math_on_a_known_delta(tmp_path):
    """Two sessions whose SHIPPED number differs by a known, hand-derived
    amount (own-reference grading is invariant to a pure shift, so the
    delta is injected as ripple, not offset — see the frozen-reference
    golden test for the offset-invariance derivation)."""
    r1 = load_banked_round(
        _make_round_dir(tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())})
    )
    r2 = load_banked_round(
        _make_round_dir(
            tmp_path, "r2",
            position_curves={"cloud_verify_02": ("onax", _flat_curve(ripple_db=1.2))},
        )
    )
    result = repeatability_spread([("a", r1), ("b", r2)])
    shipped = next(m for m in result.metrics if m.name == "shipped_linear_pool_db")
    assert shipped.values["a"] == pytest.approx(0.0, abs=1e-9)
    assert shipped.values["b"] > shipped.values["a"]
    spread = shipped.spread()
    assert spread["n"] == 2.0
    assert spread["range"] == pytest.approx(shipped.values["b"] - shipped.values["a"], abs=1e-9)
    assert spread["mean"] == pytest.approx((shipped.values["a"] + shipped.values["b"]) / 2.0, abs=1e-9)


def test_repeatability_spread_single_round_has_no_spread(tmp_path):
    r1 = load_banked_round(
        _make_round_dir(tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())})
    )
    result = repeatability_spread([("only", r1)])
    shipped = next(m for m in result.metrics if m.name == "shipped_linear_pool_db")
    assert shipped.spread() is None


def test_a_seat_carries_the_bearing_its_own_record_banked(tmp_path):
    """(S4) The views read ``position_deg`` instead of defaulting it to None.

    Before the 2026-08-24 geometry ruling a cloud seat had no bearing to read,
    so ``load_banked_round`` hardcoded ``degrees=None`` — which then made
    ``flat_spec_views``' ``angles_recorded`` false and printed "angles: NOT
    RECORDED" on rounds that DO record angles. Absence still reads as None:
    "not recorded", never zero.
    """
    banked = load_banked_round(_make_round_dir(
        tmp_path, "r1",
        position_curves={
            "cloud_verify_02": ("onax", _flat_curve()),
            "cloud_verify_03": ("onax", _flat_curve()),
        },
        position_degrees={"cloud_verify_02": 0.0},
    ))

    by_id = {p.position_id: p for p in banked.positions}
    # A banked 0 is a REAL commanded pose — the design axis — and must survive
    # as 0.0 rather than being read back as "no bearing".
    assert by_id["cloud_verify_02"].degrees == 0.0
    # …and a seat whose record carries no key is not recorded, not zero.
    assert by_id["cloud_verify_03"].degrees is None


def test_a_repeat_across_the_geometry_ruling_discloses_its_mixed_bearings(tmp_path):
    """(S4) One ``position_id``, two different seats — made VISIBLE, not refused.

    The ruling put the design axis at the front of the post-apply pose set, so
    ``cloud_verify_02`` names −7° in a pre-ruling round and 0° in a post-ruling
    one. ``repeatability_spread`` keys per-seat metrics by id, so without this
    the "spread" of two different seats reads as instrument noise.

    **Disclosed rather than blocked** — the doctrine's hard stops are component
    damage and hearing safety, and comparing across the ruling to see what the
    ruling DID is a legitimate question. So the spread is still published; the
    bearings ride beside it and ``bearings_agree()`` names the answer.
    """
    pre = load_banked_round(_make_round_dir(
        tmp_path, "pre", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
        position_degrees={"cloud_verify_02": -7.0},
    ))
    post = load_banked_round(_make_round_dir(
        tmp_path, "post", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
        position_degrees={"cloud_verify_02": 0.0},
    ))

    seat = next(
        m for m in repeatability_spread([("pre", pre), ("post", post)]).per_position
        if m.name == "cloud_verify_02"
    )

    assert seat.bearings_agree() is False
    assert seat.degrees == {"pre": -7.0, "post": 0.0}
    # Disclosure, not refusal: the number is still there to read.
    assert seat.spread() is not None
    assert seat.to_dict()["bearings_agree"] is False


def test_matching_bearings_agree_and_unrecorded_ones_answer_unknown(tmp_path):
    """(S4) The two answers that are NOT "they disagree", kept apart.

    ``True`` is only for rounds that each recorded a bearing and recorded the
    same one. A pre-ruling pair recorded none, and that is ``None`` — "nothing
    was comparable" is a different fact from "nothing disagreed", and reporting
    the second for the first is how a mixed comparison would slip through.
    """
    same_a = load_banked_round(_make_round_dir(
        tmp_path, "a", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
        position_degrees={"cloud_verify_02": 7.0},
    ))
    same_b = load_banked_round(_make_round_dir(
        tmp_path, "b", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
        position_degrees={"cloud_verify_02": 7.0},
    ))
    seat = next(
        m for m in repeatability_spread([("a", same_a), ("b", same_b)]).per_position
        if m.name == "cloud_verify_02"
    )
    assert seat.bearings_agree() is True

    # Both rounds pre-ruling: no bearing anywhere, so no comparison exists.
    old_a = load_banked_round(_make_round_dir(
        tmp_path, "old_a", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    ))
    old_b = load_banked_round(_make_round_dir(
        tmp_path, "old_b", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    ))
    old_seat = next(
        m for m in repeatability_spread([("a", old_a), ("b", old_b)]).per_position
        if m.name == "cloud_verify_02"
    )
    assert old_seat.bearings_agree() is None
    assert old_seat.degrees == {}
    assert old_seat.spread() is not None


def test_repeatability_metric_spread_uses_sample_variance_ddof1_not_population():
    """Pins the ``ddof=1`` (Bessel-corrected, SAMPLE) standard deviation,
    distinguished from the population (``ddof=0``) one on values chosen so
    the two read differently to more than rounding: for ``[1, 2, 3]``, the
    sample sd is exactly ``1.0`` and the population sd is
    ``sqrt(2/3) ≈ 0.8165`` — a mutation that dropped the ``-1`` (or changed
    ``len(vs) - 1`` to ``len(vs)``) would silently swap one for the other.
    """
    from jasper.active_speaker.crossover_v2.round_views import RepeatabilityMetric

    metric = RepeatabilityMetric("test", {"a": 1.0, "b": 2.0, "c": 3.0})
    spread = metric.spread()
    assert spread is not None
    assert spread["mean"] == pytest.approx(2.0)
    assert spread["sd"] == pytest.approx(1.0, abs=1e-9)
    # The population figure this must NOT equal, named so the distinction
    # is checkable rather than asserted from memory.
    population_sd = (sum((v - 2.0) ** 2 for v in (1.0, 2.0, 3.0)) / 3.0) ** 0.5
    assert spread["sd"] != pytest.approx(population_sd, abs=1e-9)


# --------------------------------------------------------------------------- #
# Agreement — testify/dissent classification
# --------------------------------------------------------------------------- #


def _seat_curve(position_id: str, role: str, values: np.ndarray):
    from jasper.active_speaker.crossover_v2.round_views import SeatCurve

    return SeatCurve(position_id=position_id, role=role, normalized_db=values)


def test_agreement_marks_a_feature_common_mode_when_every_seat_agrees():
    """A -6 dB dip at the same bin, same sign, same rough size, at every
    seat: sign agreement AND magnitude agreement -> COMMON-MODE.

    The dip is deep (6 dB, not 1 dB) because the optics seam's 1-octave
    window includes the dip's own bins, so a narrow, shallow dip is mostly
    cancelled by its own local average before ``agreement_table`` ever sees
    it — the same shrinkage the campaign's own detrend has. 6 dB survives
    detrending with several dB of residual, well clear of ``testify_db``.
    """
    grid = np.geomspace(400.0, 16000.0, 60)
    base = np.zeros_like(grid)
    dip_idx = 25
    seats = []
    for i, name in enumerate(["s0", "s1", "s2", "s3"]):
        curve = base.copy()
        curve[dip_idx - 1 : dip_idx + 2] -= 6.0 + 0.2 * i  # near-identical depth
        seats.append(_seat_curve(name, "onax" if i % 2 == 0 else "offax", curve))

    features = agreement_table(seats, grid, lo_hz=400.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
    assert features
    feature = min(features, key=lambda f: abs(f.center_hz - grid[dip_idx]))
    assert feature.pooled_db < 0
    assert feature.n_testify == 4
    assert feature.n_dissent == 0
    assert feature.common_mode is True


def test_agreement_reports_sign_ok_size_split_when_magnitude_disagrees():
    """Every seat dips the same direction (sign agreement holds), but one
    seat's dip is far deeper than the others (magnitude ratio > 3:1) —
    the campaign's own named failure mode (the 1400 Hz cut)."""
    grid = np.geomspace(400.0, 16000.0, 60)
    dip_idx = 25
    depths = [6.0, 6.0, 6.0, 30.0]  # last seat: 5x deeper
    seats = []
    for i, depth in enumerate(depths):
        curve = np.zeros_like(grid)
        curve[dip_idx - 1 : dip_idx + 2] -= depth
        seats.append(_seat_curve(f"s{i}", "onax", curve))

    features = agreement_table(seats, grid, lo_hz=400.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
    feature = min(features, key=lambda f: abs(f.center_hz - grid[dip_idx]))
    assert feature.n_dissent == 0
    assert feature.ratio > 3.0
    assert feature.common_mode is False


def test_agreement_reports_dissent_when_a_seat_disagrees_in_sign():
    """Three seats dip, one rises: the dissenting seat is counted and named,
    and — the campaign's own literal ``diss <= 1`` tolerance — a single
    dissenter among four testify=3 seats still leaves the feature
    COMMON-MODE (testify(3) >= AGREEMENT_TESTIFY_MIN(3), dissent(1) <=
    AGREEMENT_DISSENT_MAX(1)). What this test pins is the counting, not
    just the verdict: ``n_dissent`` must isolate exactly the one seat whose
    sign disagreed, never miscount it as a testifying seat."""
    grid = np.geomspace(400.0, 16000.0, 60)
    dip_idx = 25
    signs = [-1.0, -1.0, -1.0, +1.0]  # one seat goes the OTHER way
    seats = []
    for i, sign in enumerate(signs):
        curve = np.zeros_like(grid)
        curve[dip_idx - 1 : dip_idx + 2] += sign * 6.0
        seats.append(_seat_curve(f"s{i}", "onax", curve))

    features = agreement_table(seats, grid, lo_hz=400.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
    feature = min(features, key=lambda f: abs(f.center_hz - grid[dip_idx]))
    assert feature.n_testify == 3
    assert feature.n_dissent == 1
    assert feature.common_mode is True
    assert set(feature.seat_values_db) == {"s0", "s1", "s2", "s3"}


def test_agreement_n5_uses_the_literal_threshold_not_a_seat_count_generalisation():
    """B2's regression: at the REAL DEFAULT 5-seat cloud, a generalisation
    that scaled the testify requirement to ``len(seats) - 1`` (4 of 5) would
    refuse this feature; the campaign's own literal threshold (``testify >=
    3``) accepts it. 3 seats dip deep, 2 dip shallow but same-sign and
    within magnitude-agreement ratio of the deep three — sign agreement
    (testify=3, dissent=0) and magnitude agreement (ratio <= 3.0) both hold
    under the literal rule.
    """
    grid = np.geomspace(400.0, 16000.0, 60)
    dip_idx = 25
    depths = [6.0, 6.0, 6.0, 4.0, 4.0]
    seats = []
    for i, depth in enumerate(depths):
        curve = np.zeros_like(grid)
        curve[dip_idx - 1 : dip_idx + 2] -= depth
        seats.append(_seat_curve(f"s{i}", "onax", curve))

    features = agreement_table(seats, grid, lo_hz=400.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
    feature = min(features, key=lambda f: abs(f.center_hz - grid[dip_idx]))
    assert len(seats) == 5
    assert feature.n_testify == 3  # < 5 - 1 = 4: a seat-count-relative rule would refuse this
    assert feature.n_dissent == 0
    assert feature.ratio <= 3.0
    assert feature.common_mode is True


def test_agreement_below_testify_min_seats_is_not_evaluable_never_a_vacuous_bool():
    """Below AGREEMENT_TESTIFY_MIN seats, ``testify >= 3`` cannot be
    satisfied by construction (there are not 3 seats to testify). The
    verdict is ``None`` — a named not-evaluable state — at both 1 and 2
    seats, never a fabricated ``True`` (the old ``n_seats - 1``
    generalisation floored at 0 and returned a vacuous pass there) and
    never a fabricated ``False`` either. The measurements themselves
    (``n_testify``, ``n_dissent``) are still reported — they are not
    verdicts, and stay informative even where the verdict cannot be.
    """
    grid = np.geomspace(400.0, 16000.0, 60)
    dip_idx = 25
    for n_seats in (1, 2):
        seats = []
        for i in range(n_seats):
            curve = np.zeros_like(grid)
            curve[dip_idx - 1 : dip_idx + 2] -= 6.0
            seats.append(_seat_curve(f"s{i}", "onax", curve))
        features = agreement_table(seats, grid, lo_hz=400.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
        feature = min(features, key=lambda f: abs(f.center_hz - grid[dip_idx]))
        assert feature.n_testify == n_seats
        assert feature.common_mode is None, f"n_seats={n_seats}"


def test_agreement_dissent_above_max_fails_the_bar_even_with_testify_and_magnitude_ok():
    """Pins ``AGREEMENT_DISSENT_MAX`` specifically, isolated from the
    testify count and the magnitude ratio: 3 seats testify (meets
    ``AGREEMENT_TESTIFY_MIN``), 2 dissent (exceeds ``AGREEMENT_DISSENT_MAX
    == 1``), and the ratio is <= 3.0 (magnitude agreement holds). Only the
    dissent count can be failing the bar here — a mutation that widened
    ``AGREEMENT_DISSENT_MAX`` to 2 (or dropped the check) would flip this
    from ``False`` to ``True`` with nothing else changing.
    """
    grid = np.geomspace(400.0, 16000.0, 60)
    dip_idx = 25
    combo = [-6.0, -6.0, -6.0, 4.0, 4.0]
    seats = []
    for i, depth in enumerate(combo):
        curve = np.zeros_like(grid)
        curve[dip_idx - 1 : dip_idx + 2] += depth
        seats.append(_seat_curve(f"s{i}", "onax", curve))
    features = agreement_table(seats, grid, lo_hz=400.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
    feature = min(features, key=lambda f: abs(f.center_hz - grid[dip_idx]))
    assert feature.n_testify == 3
    assert feature.n_dissent == 2
    assert feature.ratio <= 3.0
    assert feature.common_mode is False


def test_agreement_table_refuses_an_empty_seat_list():
    grid = np.geomspace(400.0, 16000.0, 60)
    with pytest.raises(RoundViewsError, match="no seats"):
        agreement_table([], grid, lo_hz=400.0, hi_hz=16000.0)


def test_agreement_respects_the_swept_band(tmp_path):
    """A feature outside [lo_hz, hi_hz] is not reported at all."""
    grid = np.geomspace(400.0, 16000.0, 60)
    curve = np.zeros_like(grid)
    curve[5] -= 2.0  # a feature near the very bottom of the grid
    seats = [_seat_curve("s0", "onax", curve)]
    features = agreement_table(seats, grid, lo_hz=2000.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
    assert all(f.center_hz >= 2000.0 for f in features)


# --------------------------------------------------------------------------- #
# CLI wiring — jasper-round-views
# --------------------------------------------------------------------------- #


def test_cli_frozen_writes_result_into_the_target_round_dir(tmp_path):
    from jasper.cli.round_views import main

    baseline_dir = _make_round_dir(
        tmp_path, "baseline", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    target_dir = _make_round_dir(
        tmp_path, "target",
        position_curves={"cloud_verify_02": ("onax", _flat_curve(offset_db=0.5))},
    )
    rc = main(["frozen", str(baseline_dir), str(target_dir)])
    assert rc == 0
    out_path = target_dir / "frozen_reference.json"
    assert out_path.is_file()
    payload = json.loads(out_path.read_text())
    assert payload["shipped"]["onax"] == pytest.approx(0.0, abs=1e-9)
    assert payload["frozen"]["onax"] == pytest.approx(0.5, abs=1e-6)


def test_cli_per_seat_writes_seats_including_absent_verify_reason(tmp_path):
    from jasper.cli.round_views import main

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    rc = main(["per-seat", str(round_dir)])
    assert rc == 0
    payload = json.loads((round_dir / "per_seat.json").read_text())
    assert [s["position_id"] for s in payload["seats"]] == ["cloud_verify_02"]
    assert payload["verify_pose"]["included"] is False
    assert payload["verify_pose"]["reason"]


def test_cli_frequency_writes_the_shared_web_contract(tmp_path):
    from jasper.cli.round_views import main

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    rc = main(["frequency", str(round_dir)])

    assert rc == 0
    payload = json.loads((round_dir / "frequency_view.json").read_text())
    assert payload["schema"] == "jts_frequency_view/1"
    assert payload["runs"][0]["series"][0]["kind"] == "average"


def test_cli_frequency_accepts_a_standalone_analysis_document(tmp_path):
    from jasper.cli.round_views import main

    source = tmp_path / "analysis.json"
    source.write_text(json.dumps({
        "analysis": {
            "summed_response": {
                "freqs_hz": [100.0, 1000.0],
                "magnitude_db": [-24.0, -23.0],
            },
        },
    }))

    assert main(["frequency", str(source)]) == 0
    payload = json.loads((tmp_path / "frequency_view.json").read_text())
    assert payload["runs"][0]["id"] == "analysis"
    assert payload["runs"][0]["series"][0]["kind"] == "analysis"


def test_cli_frequency_rejects_a_json_document_without_curves(tmp_path, capsys):
    from jasper.cli import round_views as cli

    source = tmp_path / "notes.json"
    source.write_text(json.dumps({"notes": "not a measurement"}))

    assert cli.main(["frequency", str(source)]) == cli.EXIT_UNREADABLE
    assert json.loads(capsys.readouterr().out)["status"] == "unreadable"


def test_cli_repeat_floor_writes_the_banked_record(tmp_path):
    from jasper.active_speaker.repeat_floor import REPEAT_FLOOR_KIND, SCHEMA_VERSION
    from jasper.cli.round_views import main

    r1 = _make_round_dir(tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())})
    r2 = _make_round_dir(
        tmp_path, "r2",
        position_curves={"cloud_verify_02": ("onax", _flat_curve(ripple_db=1.2))},
    )
    out = tmp_path / "repeat-floor.json"
    assert main(["repeat-floor", str(r1), str(r2), "--out", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["kind"] == REPEAT_FLOOR_KIND
    assert payload["artifact_schema_version"] == SCHEMA_VERSION
    assert payload["n_repeats"] == 2
    assert [row["label"] for row in payload["rounds"]] == ["r1", "r2"]


def test_cli_repeat_floor_refuses_stdout_as_a_destination(tmp_path, monkeypatch):
    from jasper.cli.round_views import main

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["repeat-floor", "r1", "r2", "--out", "-"])
    assert not (tmp_path / "-").exists()


def test_cli_repeat_floor_refuses_a_single_round(tmp_path, capsys):
    from jasper.cli import round_views as cli

    r1 = _make_round_dir(tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())})
    assert cli.main(
        ["repeat-floor", str(r1), "--out", str(tmp_path / "out.json")]
    ) == cli.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


def test_cli_repeat_floor_exits_error_when_the_record_cannot_be_written(tmp_path, capsys):
    """The record is written INSIDE the guarded block: an unwritable --out is
    the WRITE exit, not a traceback out of the record's own writer."""
    from jasper.cli import round_views as cli

    r1 = _make_round_dir(tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())})
    r2 = _make_round_dir(
        tmp_path, "r2",
        position_curves={"cloud_verify_02": ("onax", _flat_curve(ripple_db=1.2))},
    )
    # A directory component that is a FILE: the write fails as an OSError for
    # any uid, unlike a chmod-based unwritable directory (root ignores it).
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    assert cli.main(
        ["repeat-floor", str(r1), str(r2), "--out", str(blocker / "x.json")]
    ) == cli.EXIT_WRITE_FAILED
    assert json.loads(capsys.readouterr().out)["status"] == "unwritable"


def test_cli_repeat_floor_requires_an_explicit_out(tmp_path):
    """No default path: this tool runs on a laptop over banked directories and
    cannot assume the speaker's own state path."""
    from jasper.cli.round_views import main

    r1 = _make_round_dir(tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())})
    with pytest.raises(SystemExit):
        main(["repeat-floor", str(r1), str(r1)])


def test_cli_reports_the_unreadable_exit_on_an_unreadable_round(tmp_path, capsys):
    from jasper.cli import round_views as cli

    rc = cli.main(["per-seat", str(tmp_path / "nope")])
    assert rc == cli.EXIT_UNREADABLE
    assert json.loads(capsys.readouterr().out)["status"] == "unreadable"


def test_cli_reports_the_write_exit_when_the_view_cannot_be_written(tmp_path, capsys):
    """An ``--out`` this process cannot create has its own named exit code,
    apart from the unreadable round's: the two send an operator to different
    places, and neither is a traceback out of the writer."""
    from jasper.cli import round_views as cli

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )

    rc = cli.main([
        "per-seat", str(round_dir), "--out", str(tmp_path / "no-such-dir" / "o.json"),
    ])

    assert rc == cli.EXIT_WRITE_FAILED
    assert json.loads(capsys.readouterr().out)["status"] == "unwritable"


def test_a_payload_the_strict_writer_rejects_is_not_a_filesystem_problem(
    tmp_path, capsys, monkeypatch
):
    """The WRITE stage claims ``OSError`` and nothing else.

    The strict writer also rejects a payload carrying ``NaN`` — co-metrics over
    partial bearing coverage builds one — and that is the run's doing, not the
    filesystem's. Sending that operator to check permissions sends them to the
    wrong place, so it falls to the refusal arm instead.
    """
    from jasper.cli import round_views as cli

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )

    def _strict(*_args, **_kwargs):
        raise ValueError("Out of range float values are not JSON compliant")

    monkeypatch.setattr(cli, "write_report", _strict)

    rc = cli.main(["per-seat", str(round_dir)])

    assert rc == cli.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


def test_an_entry_grade_over_a_packet_missing_its_block_reads_as_unreadable(
    tmp_path, capsys, monkeypatch
):
    """A packet with no ``entry_baseline`` key is corrupt, not a view declining.

    The builder always emits the block — ``available: False`` is how it reports
    a round that banked no take — so a bare ``KeyError`` there can only mean a
    packet nothing built, which is the unreadable arm by the grade's own
    docstring. Hand-built because no fixture can produce it.
    """
    from jasper.cli import round_views as cli

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    read = cli.load_banked_round

    def _without_the_block(path):
        banked = read(path)
        return dataclasses.replace(
            banked,
            packet={k: v for k, v in banked.packet.items() if k != "entry_baseline"},
        )

    monkeypatch.setattr(cli, "load_banked_round", _without_the_block)

    rc = cli.main(["entry", str(round_dir)])

    assert rc == cli.EXIT_UNREADABLE
    assert json.loads(capsys.readouterr().out)["status"] == "unreadable"


def test_cli_writes_a_live_rounds_view_beside_the_caller(tmp_path, monkeypatch):
    """A live session bundle is the daemon's directory, not the operator's.

    Defaulting inside it made the ordinary invocation — grade the round I just
    ran — depend on being able to write into the web host's own tree.
    """
    from jasper.cli.round_views import main

    session_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    ) / "bundle" / "sess1"
    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)

    assert main(["per-seat", str(session_dir)]) == 0

    assert (here / "sess1-per_seat.json").is_file()
    assert not (session_dir / "per_seat.json").exists()


# --------------------------------------------------------------------------- #
# View 0 — entry_state_grade
# --------------------------------------------------------------------------- #


ENTRY_TAKE_ID = "entry_baseline_01_01"


def _bank_entry_baseline_take(
    round_dir: Path,
    *,
    magnitude_db: np.ndarray,
    excluded: np.ndarray | None = None,
    graph_fingerprint: str = "entrygraph0001",
    freqs_hz: np.ndarray | None = None,
) -> None:
    """One write-once entry-baseline take, in the tree the store banks to.

    ``crossover_v2/<capture>/positions/<take_id>.json`` — the path
    ``contracts.BANKED_TAKE_GLOB`` selects and
    ``position_cycle.read_entry_baseline_take`` opens. Written as the real
    record is shaped rather than as the reader's narrowed view, so a change to
    either the index columns or the accept rule fails these tests.
    """
    grid = GRID if freqs_hz is None else freqs_hz
    mask = np.zeros(grid.shape, dtype=bool) if excluded is None else excluded
    positions = (
        round_dir / "bundle" / "sess1" / "evidence/v1/artifacts"
        / "crossover_v2" / "cap1" / "positions"
    )
    positions.mkdir(parents=True, exist_ok=True)
    (positions / f"{ENTRY_TAKE_ID}.json").write_text(json.dumps({
        "kind": "jts_crossover_v2_position_evidence",
        "schema_version": 1,
        "session_id": "cap1",
        "measure_kind": "baseline",
        "phase": "entry_baseline",
        "take_id": ENTRY_TAKE_ID,
        "position_id": ENTRY_TAKE_ID,
        "index": 1,
        "attempt": 1,
        "position_deg": 0,
        "role": "onax",
        "program_id": "prog-entry",
        "reference_mark": "design_axis",
        "graph_fingerprint": graph_fingerprint,
        "captured_at": "2026-08-30T00:00:00Z",
        "freqs_hz": grid.tolist(),
        "magnitude_db": magnitude_db.tolist(),
        "excluded": [bool(flag) for flag in mask],
    }))


def _round_with_entry_baseline(tmp_path: Path, **kwargs: Any) -> Path:
    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    _bank_entry_baseline_take(round_dir, **kwargs)
    return round_dir


def test_entry_grades_the_only_round_shape_that_banks_an_entry_baseline(tmp_path):
    """Issue #3478: the entry view accepts a STAGE-1 round.

    An entry baseline exists in exactly one round shape — the measure stage —
    and that stage banks no cloud group, so it has neither cloud positions nor
    a graded ``spec`` block. The loader used to refuse it on both counts,
    which made the one view whose description names what stage 1 banks
    unreachable on every rig.

    The grade a round with no post-apply spec gets is stated in NO frame, and
    the report says so on its face: there is no span in this round for a
    before to be made comparable with.
    """
    banked = load_banked_round(bank_measure_round(tmp_path))

    grade = entry_state_grade(banked)

    assert grade.available is True
    assert grade.reason == ""
    assert grade.report is not None
    assert len(grade.report.bands) == len(flat_spec.SPEC_BANDS)
    assert grade.report.trusted_floor_hz is None
    assert grade.report.trusted_ceiling_hz is None
    assert grade.program_id == "prog-entry"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["per-seat"], id="per_seat"),
        pytest.param(["repeat"], id="repeat"),
        pytest.param(["agreement"], id="agreement"),
        pytest.param(["co-metrics"], id="co_metrics"),
    ],
)
def test_the_position_graded_views_still_refuse_a_round_with_no_cloud_group(
    tmp_path, capsys, argv,
):
    """Relaxing the LOADER must not make the four position views answer.

    Each of these grades cloud seats against the round's own spec, and a
    stage-1 round banked neither. The refusal moved to the view that requires
    them; what a caller must never get is an empty table that reads as a
    graded round with nothing wrong in it.
    """
    from jasper.cli import round_views as cli

    round_dir = bank_measure_round(tmp_path)

    assert cli.main([*argv, str(round_dir)]) == cli.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


def test_the_cli_entry_and_frequency_verbs_read_a_stage_one_round(tmp_path, capsys):
    """Both verbs the campaign hit, through ``main`` and the real argv.

    ``frequency`` was refused by the same loader gate and for the same wrong
    reason: its own projector already renders an entry-baseline series from a
    packet with no positions, so nothing but the gate stood between a stage-1
    round and its curve.
    """
    from jasper.cli import round_views as cli

    round_dir = bank_measure_round(tmp_path)

    assert cli.main(["entry", str(round_dir), "--out", "-"]) == 0
    grade = json.loads(capsys.readouterr().out)
    assert grade["available"] is True
    assert grade["round_ordinal"] == 1

    assert cli.main(["frequency", str(round_dir), "--out", "-"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert [s["kind"] for s in view["runs"][0]["series"]] == ["entry_baseline"]


def test_the_entry_state_is_graded_by_the_shipped_evaluator(tmp_path):
    """The door's whole contract: it CONSUMES the grading, never repeats it.

    Asserted against an independent ``evaluate_flat_spec`` call on the same
    inputs — the take's own curve and mask, in the round's own frame — so the
    door cannot pass by returning plausible numbers of its own. Field-for-field
    on the report, not a spot check: a door that graded the right curve in the
    WRONG frame would agree on the bands and disagree on the reference.
    """
    curve = _flat_curve(ripple_db=3.0)
    banked = load_banked_round(_round_with_entry_baseline(tmp_path, magnitude_db=curve))

    grade = entry_state_grade(banked)

    assert grade.available is True
    assert grade.reason == ""
    expected = evaluate_flat_spec(
        GRID, curve, np.zeros(GRID.shape, dtype=bool),
        smoothing_fraction=banked.report.smoothing_fraction,
        trusted_floor_hz=banked.report.trusted_floor_hz,
        trusted_ceiling_hz=banked.report.trusted_ceiling_hz,
    )
    assert grade.report is not None
    assert grade.report.to_dict() == expected.to_dict()


def test_a_re_grade_reads_the_rounds_room_floor_back_instead_of_re_deriving_it(
    tmp_path,
):
    """#3502 — a re-evaluation states the ROUND's room floor, never its own.

    This door grades a stored take in the round's own frame. The room floor is
    part of that frame: the round pooled it from the seats it actually
    measured, and a floor recomputed at this door would be a second opinion
    about one room, stated over a curve that never saw it. So it is read off
    the banked report — provenance included, because a floor that arrived here
    as ``declared_geometry`` may not print as measured downstream.
    """
    round_dir = _round_with_entry_baseline(tmp_path, magnitude_db=_flat_curve())
    banked_path = next(round_dir.glob("bundle/*/evidence/v1/artifacts/**/cloud_verify.json"))
    cloud = json.loads(banked_path.read_text())
    cloud["spec"]["entanglement_floor_hz"] = 610.0
    cloud["spec"]["entanglement_floor_source"] = "declared_geometry"
    banked_path.write_text(json.dumps(cloud))
    banked = load_banked_round(round_dir)
    assert banked.report is not None
    assert banked.report.entanglement_floor_hz == 610.0

    report = entry_state_grade(banked).report

    assert report is not None
    assert report.entanglement_floor_hz == banked.report.entanglement_floor_hz
    assert report.entanglement_floor_source == banked.report.entanglement_floor_source
    # It MARKS and does not clamp: the graded edges are the round's, untouched.
    assert [b.graded_lo_hz for b in report.bands] == [
        b.graded_lo_hz for b in banked.report.bands
    ]
    assert any(b.room_entangled_below_hz == 610.0 for b in report.bands)


def test_the_entry_grade_carries_a_per_band_table(tmp_path):
    """The same per-band rows a round's own ``spec`` block carries.

    Structural, not a spot value: every ``SPEC_BANDS`` row is answered for, and
    each row states its own tolerance and verdict. That is what makes this
    table readable beside a round's without a translation step.
    """
    banked = load_banked_round(
        _round_with_entry_baseline(tmp_path, magnitude_db=_flat_curve())
    )

    report = entry_state_grade(banked).report

    assert report is not None
    assert len(report.bands) == len(flat_spec.SPEC_BANDS)
    assert [b.tolerance_db for b in report.bands] == [
        row[2] for row in flat_spec.SPEC_BANDS
    ]
    assert all(band.evaluable for band in report.bands)
    assert report.overall_passed is True


def test_a_tilted_entry_state_fails_the_band_it_is_tilted_in(tmp_path):
    """Discriminating: the grade tracks the curve, not the fixture.

    A treble shelf far outside the top band's tolerance must fail THAT band and
    leave the others passing — a door returning a canned "passed" report, or
    grading somebody else's curve, cannot produce this shape.
    """
    curve = _flat_curve()
    curve[GRID >= 8000.0] += 6.0
    banked = load_banked_round(_round_with_entry_baseline(tmp_path, magnitude_db=curve))

    report = entry_state_grade(banked).report

    assert report is not None
    assert report.overall_passed is False
    by_edge = {band.f_lo_hz: band for band in report.bands}
    assert by_edge[8000.0].passed is False
    assert all(
        band.passed is True for lo, band in by_edge.items() if lo != 8000.0
    )


def test_the_entry_grade_reads_the_takes_OWN_exclusion_mask(tmp_path):
    """The mask belongs to this capture, not to the round's other one.

    A bin the entry-baseline screen flagged must not be graded. Pinned with a
    curve whose ONLY spec violation sits under the mask: unmasked it fails,
    masked it passes, so a door that dropped the mask (or reached for the
    round's post-apply exclusions instead) is a different answer, not a
    rounding difference.
    """
    curve = _flat_curve()
    spike = GRID >= 8000.0
    curve[spike] += 6.0

    unmasked = load_banked_round(
        _round_with_entry_baseline(tmp_path / "a", magnitude_db=curve)
    )
    masked = load_banked_round(
        _round_with_entry_baseline(tmp_path / "b", magnitude_db=curve, excluded=spike)
    )

    assert entry_state_grade(unmasked).report.overall_passed is False
    masked_report = entry_state_grade(masked).report
    assert masked_report is not None
    # The masked band has no evidence left, so it is UNEVALUABLE — never a
    # silent pass. That is `BandResult`'s own contract and this door inherits
    # it rather than restating it.
    by_edge = {band.f_lo_hz: band for band in masked_report.bands}
    assert by_edge[8000.0].evaluable is False
    assert by_edge[8000.0].passed is None
    assert by_edge[250.0].passed is True


def test_the_entry_grade_names_WHICH_entry_state_it_graded(tmp_path):
    """An unattributed table is not a disclosure.

    The first round's entry graph is the declarations-derived config a fresh
    box wears; a later round's is whatever the previous round left playing.
    The fingerprint is what tells them apart, so it rides on the result.
    """
    banked = load_banked_round(
        _round_with_entry_baseline(
            tmp_path, magnitude_db=_flat_curve(), graph_fingerprint="fresh0000beef",
        )
    )

    grade = entry_state_grade(banked)

    assert grade.graph_fingerprint == "fresh0000beef"
    assert grade.program_id == "prog-entry"
    assert grade.reference_mark == "design_axis"
    assert grade.artifact_ref == ENTRY_TAKE_ID
    assert grade.to_dict()["graph_fingerprint"] == "fresh0000beef"


def test_a_round_that_banked_no_entry_baseline_says_so_with_a_reason(tmp_path):
    """The honest door for the case it cannot answer.

    Retention is fail-soft and never costs the household a retake, so "no take"
    is a fact to report rather than a failure to raise. It must arrive as a
    NAMED reason with no report beside it — never an empty table that reads as
    a clean bill of health.
    """
    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )

    grade = entry_state_grade(load_banked_round(round_dir))

    assert grade.available is False
    assert grade.report is None
    assert grade.reason  # named, never a bare False
    assert grade.to_dict()["report"] is None


def test_a_banked_take_whose_curve_does_not_rehydrate_is_not_graded(tmp_path):
    """A mask shorter than its curve is unreadable, not gradeable.

    ``EntryBaseline.from_dict`` owns that rule; this pins that the door takes
    its ``None`` as a refusal to grade rather than pushing a length-disagreeing
    pair into the evaluator.
    """
    round_dir = _round_with_entry_baseline(
        tmp_path, magnitude_db=_flat_curve(),
        excluded=np.zeros(GRID.shape[0] - 3, dtype=bool),
    )

    grade = entry_state_grade(load_banked_round(round_dir))

    assert grade.available is False
    assert grade.report is None
    assert grade.reason == ENTRY_STATE_UNREADABLE


def test_the_cli_entry_verb_writes_the_grade_beside_the_evidence(tmp_path, capsys):
    """The DOOR, not just the view — through ``main`` and the real argv.

    A product view nothing can reach is not a door: before this verb the entry
    state could only be graded by an operator calling ``evaluate_flat_spec`` by
    hand. Drives the console script end to end and asserts the artifact it
    leaves behind.
    """
    from jasper.cli import round_views as cli

    round_dir = _round_with_entry_baseline(tmp_path, magnitude_db=_flat_curve())

    assert cli.main(["entry", str(round_dir)]) == 0

    written = json.loads((round_dir / "entry_state_grade.json").read_text())
    assert written["available"] is True
    assert written["graph_fingerprint"] == "entrygraph0001"
    assert len(written["report"]["bands"]) == len(flat_spec.SPEC_BANDS)


def test_the_cli_entry_verb_exits_0_when_there_is_nothing_to_grade(tmp_path):
    """"No gradeable take" is an ANSWER, not an unreadable round.

    A caller can tell "I looked, and this round banked none" from "I could not
    look" by the exit code alone.
    """
    from jasper.cli import round_views as cli

    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )

    assert cli.main(["entry", str(round_dir)]) == 0

    written = json.loads((round_dir / "entry_state_grade.json").read_text())
    assert written["available"] is False
    assert written["report"] is None
    assert written["reason"]


def _write_state(round_dir: Path, payload: dict[str, Any]) -> None:
    (round_dir / "state.json").write_text(json.dumps(payload))


def test_the_entry_grade_attributes_the_round_and_its_ordinal_epoch(tmp_path):
    """An unattributed table is not a disclosure.

    "The entry state was this flat" means one thing at round 1 of a fresh box
    and another at round 1 after a republish reset the count — so the ordinal
    and the epoch it counts in ride on the result and its payload.
    """
    round_dir = _round_with_entry_baseline(tmp_path, magnitude_db=_flat_curve())
    _write_state(round_dir, {
        "round_receipt": {"round_ordinal": 2}, "round_ordinal_epoch": 3,
    })

    grade = entry_state_grade(load_banked_round(round_dir))

    assert grade.round_ordinal == 2
    assert grade.round_ordinal_epoch == 3
    payload = grade.to_dict()
    assert payload["round_ordinal"] == 2
    assert payload["round_ordinal_epoch"] == 3


def test_an_unrecorded_ordinal_reads_as_not_recorded_never_zero(tmp_path):
    """``None`` and ``0`` are different facts, and the epoch's whole meaning
    turns on the difference: ``0`` is "never reset", which a round that simply
    banked no state file has said nothing about.

    ``bool`` is rejected too — a hand-edited ``true`` must not publish as
    epoch 1.
    """
    no_state = _round_with_entry_baseline(tmp_path / "a", magnitude_db=_flat_curve())
    grade = entry_state_grade(load_banked_round(no_state))
    assert grade.round_ordinal is None
    assert grade.round_ordinal_epoch is None

    booly = _round_with_entry_baseline(tmp_path / "b", magnitude_db=_flat_curve())
    _write_state(booly, {
        "round_receipt": {"round_ordinal": True}, "round_ordinal_epoch": True,
    })
    boolean = entry_state_grade(load_banked_round(booly))
    assert boolean.round_ordinal is None
    assert boolean.round_ordinal_epoch is None


def test_the_cli_counts_an_unevaluable_band_apart_from_a_failing_one(tmp_path, capsys):
    """An UNEVALUABLE band is not a failing band.

    A band whose every bin the take's own gate clamped away has no evidence —
    ``passed is None``, never ``False`` — and a summary that counted it as
    failing would report a band nobody could measure as one that measured
    badly. Driven through the console script, on the same masked fixture the
    product-level mask test uses.
    """
    from jasper.cli import round_views as cli

    curve = _flat_curve()
    spike = GRID >= 8000.0
    curve[spike] += 6.0
    round_dir = _round_with_entry_baseline(
        tmp_path, magnitude_db=curve, excluded=spike,
    )

    assert cli.main(["entry", str(round_dir)]) == 0

    summary = capsys.readouterr().err
    assert "1 unevaluable" in summary
    assert "0 outside target" in summary


# --------------------------------------------------------------------------- #
# Audibility-weighted co-metrics (ticket 6.13 / ADR-0202)
# --------------------------------------------------------------------------- #


def _bank_lateral_pose(
    session_dir: Path, *, take_id: str, position_deg: int,
    curves: list[dict[str, Any]], vertical_deg: int = 0,
    capture: str = "wired-TEST",
) -> None:
    """Directly write a banked ``positions/<take_id>.json`` lateral-pose
    take — the exact shape :func:`~jasper.active_speaker.crossover_v2.record_index.bundle_measurements`
    and :func:`~jasper.active_speaker.crossover_v2.position_cycle.read_take_curves`
    read, real-shaped without going through the retention engine. Mirrors
    ``test_crossover_v2_feature_classifier.py``'s own fixture builder for
    the same take shape.
    """
    positions_dir = (
        session_dir / "evidence/v1/artifacts/crossover_v2" / capture / "positions"
    )
    positions_dir.mkdir(parents=True, exist_ok=True)
    (positions_dir / f"{take_id}.json").write_text(json.dumps({
        "kind": POSITION_EVIDENCE_KIND,
        "phase": PHASE_LATERAL,
        "position_deg": position_deg,
        "vertical_deg": vertical_deg,
        "curves": curves,
    }))


def _summed_curve(freqs_hz: np.ndarray, magnitude_db: np.ndarray) -> dict[str, Any]:
    return {
        "role": "summed",
        "band_hz": [20.0, 20000.0],
        "freqs_hz": [float(v) for v in freqs_hz],
        "magnitude_db": [float(v) for v in magnitude_db],
        "phase_deg": [0.0] * len(freqs_hz),
    }


def test_pooled_window_horizontal_power_averages_a_hand_computed_two_curve_case(tmp_path):
    """Two bearings, one 'summed' curve each, fully covering the grid.

    A (0 deg): 0 dB -> power 1.0.  B (+7 deg): 10*log10(3) dB -> power 3.0.
    Power mean = 2.0 -> pooled dB = 10*log10(2) ~= 3.0103 dB, at every bin.

    A third stop shares A's bearing but sits 10 deg above mark height, at a
    wild power (100.0): this pool is the HORIZONTAL window, so a raised seat
    is skipped rather than bucketed under the bearing it shares -- it moves
    neither the curve count, the bearing set, nor the arithmetic.
    """
    session_dir = tmp_path / "bundle" / "sess1"
    grid = np.array([1000.0, 2000.0])
    a_db, b_db = 0.0, 10.0 * np.log10(3.0)
    _bank_lateral_pose(
        session_dir, take_id="lateral_00_a01", position_deg=0,
        curves=[_summed_curve(grid, np.full_like(grid, a_db))],
    )
    _bank_lateral_pose(
        session_dir, take_id="lateral_02_a01", position_deg=7,
        curves=[_summed_curve(grid, np.full_like(grid, b_db))],
    )
    _bank_lateral_pose(
        session_dir, take_id="lateral_04_a01", position_deg=0, vertical_deg=10,
        curves=[_summed_curve(grid, np.full_like(grid, 20.0))],
    )

    result = pooled_window_horizontal(session_dir, grid_hz=grid)

    assert result is not None
    assert result.bearings_deg == (0.0, 7.0)
    assert result.n_curves == 2
    expected_db = 10.0 * np.log10(2.0)
    assert result.magnitude_db == pytest.approx([expected_db, expected_db])


def test_pooled_window_horizontal_pools_a_revisited_bearing_before_pooling_bearings(tmp_path):
    """A bearing visited by two stops must not outweigh one visited once,
    and a superseded retake must not contribute at all.

    Two DISTINCT stops at 0 deg (a drift re-visit): powers 0.5 and 1.5,
    whose power MEAN is exactly 1.0 (0 dB) -- the same value the single
    +7 deg curve was in the two-curve case above, so two-stage pooling
    (average the bearing's stops, THEN average across bearings) answers
    10*log10(2) dB. Naive single-stage pooling (flat-average all three
    curves, powers [0.5, 1.5, 3.0]) would instead give
    10*log10(5/3) ~= 2.2185 dB. The 0.5 stop is additionally banked with a
    superseded earlier attempt at a wild power (100.0): were retakes pooled
    instead of superseded, no two-stage/one-stage arithmetic could land on
    the expected value either.
    """
    session_dir = tmp_path / "bundle" / "sess1"
    grid = np.array([1000.0])
    for take_id, power in (
        ("lateral_00_a01", 100.0),  # superseded by a02 below
        ("lateral_00_a02", 0.5),
        ("lateral_04_a01", 1.5),  # second stop, same 0 deg bearing
    ):
        _bank_lateral_pose(
            session_dir, take_id=take_id, position_deg=0,
            curves=[_summed_curve(grid, 10.0 * np.log10(np.array([power])))],
        )
    _bank_lateral_pose(
        session_dir, take_id="lateral_02_a01", position_deg=7,
        curves=[_summed_curve(grid, np.array([10.0 * np.log10(3.0)]))],
    )

    result = pooled_window_horizontal(session_dir, grid_hz=grid)

    assert result is not None
    assert result.bearings_deg == (0.0, 7.0)
    assert result.n_curves == 3
    assert result.magnitude_db == pytest.approx([10.0 * np.log10(2.0)])


def test_pooled_window_horizontal_is_none_when_no_lateral_pose_banked_a_summed_curve(tmp_path):
    session_dir = tmp_path / "bundle" / "sess1"
    session_dir.mkdir(parents=True)
    assert pooled_window_horizontal(session_dir, grid_hz=np.array([1000.0])) is None


def test_pooled_window_horizontal_ignores_a_per_driver_role(tmp_path):
    """Only ``role == "summed"`` counts — an isolated driver branch is not
    the speaker's composed response."""
    session_dir = tmp_path / "bundle" / "sess1"
    grid = np.array([1000.0])
    _bank_lateral_pose(
        session_dir, take_id="lateral_00_a01", position_deg=0,
        curves=[{
            "role": "tweeter", "band_hz": [20.0, 20000.0],
            "freqs_hz": [1000.0], "magnitude_db": [0.0], "phase_deg": [0.0],
        }],
    )
    assert pooled_window_horizontal(session_dir, grid_hz=grid) is None


def test_audibility_co_metrics_reports_on_axis_and_discloses_an_absent_pooled_window(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    banked = load_banked_round(round_dir)

    result = audibility_co_metrics(banked)

    assert result.round_dir == str(round_dir)
    assert result.on_axis is not None
    assert result.on_axis.nbd_db == pytest.approx(0.0, abs=1e-9)
    assert result.on_axis.sm_r2 == pytest.approx(1.0, abs=1e-9)
    assert result.on_axis_reason == ""
    assert result.pooled_window is None
    assert result.pooled_window_reason
    assert result.pooled_window_bearings_deg == ()


def test_audibility_co_metrics_reports_the_pooled_window_when_the_round_banked_one(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    banked = load_banked_round(round_dir)
    _bank_lateral_pose(
        banked.session_dir, take_id="lateral_00_a01", position_deg=0,
        curves=[_summed_curve(GRID, _flat_curve())],
    )

    result = audibility_co_metrics(banked)

    assert result.pooled_window is not None
    assert result.pooled_window.nbd_db == pytest.approx(0.0, abs=1e-9)
    assert result.pooled_window.sm_r2 == pytest.approx(1.0, abs=1e-9)
    assert result.pooled_window_reason == ""
    assert result.pooled_window_bearings_deg == (0.0,)


def test_audibility_co_metrics_discloses_an_absent_on_axis_position(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("offax", _flat_curve())},
    )
    banked = load_banked_round(round_dir)

    result = audibility_co_metrics(banked)

    assert result.on_axis is None
    assert result.on_axis_reason


def test_cli_co_metrics_writes_the_result(tmp_path):
    from jasper.cli.round_views import main

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    rc = main(["co-metrics", str(round_dir)])
    assert rc == 0
    payload = json.loads((round_dir / "audibility_co_metrics.json").read_text())
    assert payload["on_axis"]["nbd_db"] == pytest.approx(0.0, abs=1e-9)
    assert payload["on_axis"]["sm_r2"] == pytest.approx(1.0, abs=1e-9)
    assert payload["pooled_window"] is None
    assert payload["pooled_window_reason"]


# --------------------------------------------------------------------------- #
# the gate ladder, stamped onto the round's own spec verdict
# --------------------------------------------------------------------------- #

#: Two rungs, not the default seven: this suite is proving the wiring, and the
#: ladder's own physics is measured in ``test_crossover_v2_gate_sweep.py``.
SWEEP_RUNGS_MS = (5.0, 20.0)


def _curve_dipping_at(hz: float, *, depth_db: float = -3.0) -> np.ndarray:
    """A flat curve with its single worst bin at the grid bin nearest ``hz``."""
    curve = np.full(GRID.shape, REFERENCE_DB, dtype=float)
    curve[int(np.argmin(np.abs(GRID - hz)))] += depth_db
    return curve


def _banked_for_sweep(round_dir: Path, report) -> BankedRound:
    """One round's captures and one round's verdict, as the function takes them.

    Built directly rather than through :func:`load_banked_round`: the captures
    the sweep reads and the evidence packet the loader parses live in two
    different trees under ``bundle/``, and only one session directory may.
    """
    return BankedRound(
        round_dir=round_dir,
        inputs=round_inputs_mod.RoundInputs(
            session_dir=round_dir,
            state_path=None,
            design_draft_path=None,
            applied_profile_path=None,
            repeat_floor_path=None,
            declared_geometry_path=None,
            banked=True,
        ),
        positions=(),
        curve_grid_hz=GRID,
        report=report,
        packet={},
    )


@pytest.fixture(scope="module")
def swept_low_band(tmp_path_factory):
    """A three-pose round whose graded low band is worst at the feature the
    captures actually carry, swept and stamped."""
    round_dir = bank_capture_round(
        tmp_path_factory.mktemp("swept"),
        [_pose_ir(i, late_copy_ms=8.0 + 0.9 * i) for i in range(3)],
    )
    report = evaluate_flat_spec(
        GRID, _curve_dipping_at(FEATURE_HZ), np.zeros(GRID.shape, dtype=bool),
    )
    stamped = spec_with_gate_sensitivity(
        _banked_for_sweep(round_dir, report), rungs_ms=SWEEP_RUNGS_MS,
    )
    return report, stamped


def test_the_spec_verdict_carries_the_sweep_at_each_bands_worst_bin(swept_low_band):
    """The verdict names the bin and the ladder answers at THAT bin, with the
    frame those numbers are only meaningful inside."""
    report, stamped = swept_low_band

    low = stamped.bands[0]
    assert low.max_deviation_hz == report.bands[0].max_deviation_hz
    assert low.gate_sensitivity_note is None
    assert np.isfinite(low.sigma_growth_ratio)
    assert np.isfinite(low.gate_sensitivity_db)
    assert low.n_valid_rungs == len(SWEEP_RUNGS_MS)
    # Real builtins, never `np.float64`/`np.int64`: this report is persisted
    # through `json.dumps`, which the numpy scalars silently break.
    assert type(low.sigma_growth_ratio) is float
    assert type(low.gate_sensitivity_db) is float
    assert type(low.n_valid_rungs) is int

    # The room/speaker call itself: this round's varying late-reflection copy
    # per pose is exactly the across-pose-sigma-growth signature (#3495), so
    # the ladder calls it MOVED via the sigma-growth route.
    assert low.gate_window_verdict == WINDOW_MOVED
    assert ROUTE_SIGMA_GROWTH in low.gate_window_verdict_reasons
    assert type(low.gate_window_verdict_reasons) is tuple

    assert stamped.gate_sweep_frame is not None
    assert stamped.gate_sweep_frame["rungs_ms"] == list(SWEEP_RUNGS_MS)


def test_stamping_the_sweep_moves_no_grade(swept_low_band):
    """Disclosure only. Strip the eight new fields and the report is the one
    `evaluate_flat_spec` produced, band for band and verdict for verdict.
    """
    from dataclasses import replace

    report, stamped = swept_low_band

    stripped = replace(
        stamped,
        bands=tuple(
            replace(
                band, gate_sensitivity_db=None, sigma_growth_ratio=None,
                n_valid_rungs=None, gate_sensitivity_note=None,
                gate_sensitivity_detail=None, gate_window_verdict=None,
                gate_window_verdict_reasons=None,
            )
            for band in stamped.bands
        ),
        gate_sweep_frame=None,
    )
    assert stripped == report


def test_a_single_pose_round_is_named_as_not_swept(tmp_path):
    """Across-pose sigma has no meaning on one pose, so there is no number and
    the reason says which kind of nothing it is."""
    round_dir = bank_capture_round(tmp_path, [_pose_ir(0, late_copy_ms=8.0)])
    report = evaluate_flat_spec(
        GRID, _curve_dipping_at(FEATURE_HZ), np.zeros(GRID.shape, dtype=bool),
    )

    stamped = spec_with_gate_sensitivity(
        _banked_for_sweep(round_dir, report), rungs_ms=SWEEP_RUNGS_MS,
    )

    assert all(band.sigma_growth_ratio is None for band in stamped.bands)
    assert all(band.n_valid_rungs is None for band in stamped.bands)
    assert all(
        band.gate_sensitivity_note == NOT_SWEPT_SINGLE_POSE
        for band in stamped.bands
    )
    assert stamped.gate_sweep_frame is None


def test_a_band_with_no_worst_bin_is_told_apart_from_a_round_with_no_captures(
    tmp_path,
):
    """Two different kinds of nothing, and a reader must not read either as the
    other. The captures are never opened for the band that has no bin to ask
    about, and an unreadable round is still a graded round.
    """
    report = evaluate_flat_spec(
        GRID, _curve_dipping_at(FEATURE_HZ), np.zeros(GRID.shape, dtype=bool),
        trusted_ceiling_hz=8000.0,
    )
    assert report.bands[-1].max_deviation_hz is None  # the ceiling took it whole

    stamped = spec_with_gate_sensitivity(
        _banked_for_sweep(tmp_path, report), rungs_ms=SWEEP_RUNGS_MS,
    )

    assert stamped.bands[-1].gate_sensitivity_note == NOT_SWEPT_BAND_NOT_EVALUABLE
    assert stamped.bands[0].gate_sensitivity_note == NOT_SWEPT_CAPTURES_UNREADABLE
    # The bucket slug is one word; the detail behind it still names which
    # RoundCapturesRefused this round actually hit.
    assert stamped.bands[0].gate_sensitivity_detail["reason"] == REFUSE_NO_CAPTURES
    assert stamped.gate_sweep_frame is None
    assert stamped.overall_passed == report.overall_passed


def test_cli_spec_sweep_writes_the_verdict_carrying_its_gate_read(tmp_path):
    """The door the driving LLM actually reaches: one round in, the graded spec
    with room-or-speaker answered at each band's own worst bin out.
    """
    import shutil

    from jasper.cli.round_views import main

    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={"cloud_verify_02": ("onax", _flat_curve())},
        combined_db=_curve_dipping_at(FEATURE_HZ),
    )
    # The captures live INSIDE the session bundle, where a real banked round
    # carries them, so one directory answers both readers: the evidence packet
    # for the verdict and the raw WAVs for the ladder.
    captures = bank_capture_round(
        tmp_path / "captures",
        [_pose_ir(i, late_copy_ms=8.0 + 0.9 * i) for i in range(3)],
    )
    shutil.copytree(
        captures / "bundle" / "b0", round_dir / "bundle" / "sess1", dirs_exist_ok=True,
    )

    rc = main(
        ["spec-sweep", str(round_dir), "--rungs-ms", "5", "20"],
    )

    assert rc == 0
    payload = json.loads((round_dir / "spec_gate_sensitivity.json").read_text())
    assert payload["round_dir"] == str(round_dir)
    spec = payload["spec"]
    assert spec["gate_sweep_frame"]["rungs_ms"] == [5.0, 20.0]

    low = spec["bands"][0]
    assert low["gate_sensitivity_note"] is None
    assert low["n_valid_rungs"] == 2
    assert np.isfinite(low["sigma_growth_ratio"])
    assert np.isfinite(low["gate_sensitivity_db"])
    # The verdict is the round's OWN, re-read and not re-graded.
    banked = load_banked_round(round_dir).graded_report
    assert spec["overall_passed"] == banked.overall_passed
    assert low["max_deviation_hz"] == banked.bands[0].max_deviation_hz
    assert low["passed"] == banked.bands[0].passed
