# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behaviour pins for the forward model: a predicted sum nothing has played.

Closed-form fixtures throughout — two branches whose analytic sum is known
before the model runs — so a pin says the arithmetic is RIGHT rather than that
it is unchanged. One pin crosses instruments: the same synthetic pair read for
null depth through this model and through the delay landscape must agree, or
the two predictors of one physical quantity have drifted.
"""

import json
import math
import re
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT
from jasper.active_speaker.crossover_v2 import delay_landscape
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.forward_model import (
    ACCEPTANCE_JUDGED,
    ACCEPTANCE_NOT_RUN,
    REFUSAL_GRID_DISAGREES,
    BranchPair,
    ForwardModelError,
    PredictedSum,
    SummationCandidate,
    load_branch_pair,
    predict_sum,
    predicted_minus_measured_db,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_MEASURE
from jasper.active_speaker.crossover_v2.position_cycle import parse_curve_complex
from jasper.active_speaker.delay_sweep import sweep_spec
from jasper.audio_measurement.analysis import crossover_null_depth_db
from jasper.cli._refusal import EXIT_REFUSED
from jasper.cli.round_views import (
    ACCEPTANCE_RUNS,
    REASON_REFUSED,
    build_parser,
    main as cli_main,
)

from tests.crossover_v2_banked_round import (
    SOLO_BAND_HZ,
    bank_measure_round,
    bank_verify_round,
)

FC_HZ = 1800.0
BAND = (200.0, 12000.0)


def _grid(points: int = 512, band=BAND) -> np.ndarray:
    return np.linspace(band[0], band[1], points)


def _banked(role: str, tf: np.ndarray, freqs: np.ndarray, band=BAND) -> dict:
    """One curve in ``spatial.pose_curve_record``'s exact banked shape."""

    return {
        "role": role,
        "band_hz": [float(band[0]), float(band[1])],
        "freqs_hz": [float(hz) for hz in freqs],
        "magnitude_db": [float(db) for db in 20.0 * np.log10(np.abs(tf))],
        "phase_deg": [float(deg) for deg in np.degrees(np.angle(tf))],
    }


def _lr4(freqs: np.ndarray, *, highpass: bool) -> np.ndarray:
    """One LR4 branch: an inverted, aligned pair cancels hard at Fc."""

    s = 1j * (np.asarray(freqs, dtype=float) / FC_HZ)
    butter2 = (s**2 if highpass else 1.0) / (s**2 + math.sqrt(2.0) * s + 1.0)
    return butter2**2


def _pair(
    woofer_tf: np.ndarray,
    tweeter_tf: np.ndarray,
    freqs: np.ndarray,
    *,
    woofer_band=BAND,
    tweeter_band=BAND,
) -> BranchPair:
    return BranchPair(
        freqs_hz=freqs,
        woofer_role="woofer",
        tweeter_role="tweeter",
        woofer_tf=woofer_tf,
        tweeter_tf=tweeter_tf,
        band_hz_by_role={"woofer": woofer_band, "tweeter": tweeter_band},
        take_path="positions/p0_a01.json",
    )


def _flat_pair(freqs: np.ndarray) -> BranchPair:
    ones = np.ones(freqs.size, dtype=complex)
    return _pair(ones, ones.copy(), freqs)


def _bank_take(
    tmp_path: Path,
    curves,
    *,
    phase: str = PHASE_MEASURE,
    position_deg: int = 0,
    take_id: str = "p0_a01",
) -> Path:
    """A bundle carrying one banked take, at the path the store writes.

    No index file: ``bundle_measurements`` rescans the take files on disk,
    which is what a hand-built fixture like this one relies on.
    """

    positions = (
        tmp_path / EVIDENCE_ROOT / "artifacts" / "crossover_v2" / "capture-1"
        / "positions"
    )
    positions.mkdir(parents=True, exist_ok=True)
    (positions / f"{take_id}.json").write_text(
        json.dumps({
            "schema_version": 1,
            "kind": POSITION_EVIDENCE_KIND,
            "phase": phase,
            "take_id": take_id,
            "position_deg": position_deg,
            "curves": curves,
        }),
        encoding="utf-8",
    )
    return tmp_path


# --------------------------------------------------------------------------- #
# the arithmetic
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("polarity_sign", "residual_delay_us", "woofer_db", "tweeter_db"),
    [
        pytest.param(1, 0.0, 0.0, 0.0, id="in_phase_aligned"),
        pytest.param(-1, 0.0, 0.0, 0.0, id="inverted_aligned"),
        pytest.param(1, 120.0, 0.0, 0.0, id="in_phase_delayed"),
        pytest.param(-1, 120.0, 0.0, 0.0, id="inverted_delayed"),
        pytest.param(1, 0.0, -6.0206, 0.0, id="woofer_trimmed"),
        pytest.param(-1, -75.0, -3.0, -1.5, id="both_trimmed_negative_delay"),
    ],
)
def test_the_predicted_sum_is_the_analytic_sum_of_the_two_branches(
    polarity_sign, residual_delay_us, woofer_db, tweeter_db,
) -> None:
    """``|g_w·W + sign·g_t·T·e^(-j2πfτ)|`` in dB, on two flat unity branches.

    Flat branches make the sum closed-form, so this pins the composition's
    every axis — polarity, residual delay, both trims — against arithmetic
    written out independently rather than against a recorded output.
    """

    freqs = _grid()
    predicted = predict_sum(
        _flat_pair(freqs),
        SummationCandidate(
            trim_db_by_role={"woofer": woofer_db, "tweeter": tweeter_db},
            polarity_sign=polarity_sign,
            residual_delay_us=residual_delay_us,
        ),
    )

    expected = np.abs(
        10.0 ** (woofer_db / 20.0)
        + polarity_sign
        * 10.0 ** (tweeter_db / 20.0)
        * np.exp(-1j * 2.0 * np.pi * freqs * residual_delay_us * 1e-6)
    )
    assert isinstance(predicted, PredictedSum)
    assert np.allclose(
        predicted.predicted_db, 20.0 * np.log10(np.maximum(expected, 1e-12))
    )
    assert np.array_equal(predicted.freqs_hz, freqs)
    assert predicted.sum_band_hz == BAND


def test_a_candidate_filter_enters_the_sum_at_its_own_known_gain() -> None:
    """A peaking biquad is exactly ``10^(gain/20)`` at its centre frequency.

    Non-circular on purpose: the expectation is that closed-form value rather
    than a second call to ``chain_response``, so this pins that the candidate's
    filters reach the sum at all, on the right branch, and with the right gain.
    """

    freqs = _grid()
    gain_db = 6.0
    centre = float(freqs[np.argmin(np.abs(freqs - 4000.0))])
    predicted = predict_sum(
        _flat_pair(freqs),
        SummationCandidate(
            filters_by_role={
                "woofer": [
                    {"biquad_type": "Peaking", "freq": centre, "q": 1.0,
                     "gain": gain_db}
                ]
            }
        ),
    )

    at_centre = predicted.predicted_db[np.argmin(np.abs(freqs - centre))]
    assert at_centre == pytest.approx(
        20.0 * np.log10(10.0 ** (gain_db / 20.0) + 1.0)
    )


def test_a_branch_contributes_nothing_outside_its_own_swept_band() -> None:
    """Outside its swept band a driver's banked sample is noise, so the model
    contributes exactly zero from it — the sum there is the other branch alone."""

    freqs = _grid()
    ones = np.ones(freqs.size, dtype=complex)
    tweeter_band = (2000.0, BAND[1])
    predicted = predict_sum(
        _pair(ones, ones.copy(), freqs, tweeter_band=tweeter_band),
        SummationCandidate(),
    )

    below = freqs < tweeter_band[0]
    assert np.allclose(predicted.predicted_db[below], 0.0)
    assert np.allclose(predicted.predicted_db[~below], 20.0 * np.log10(2.0))
    assert predicted.sum_band_hz == BAND


# --------------------------------------------------------------------------- #
# the cross-instrument pin
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("relative_delay_us", [0.0, 120.0, -200.0])
def test_the_predicted_null_depth_agrees_with_the_delay_landscape(
    relative_delay_us,
) -> None:
    """Two predictors of ONE physical quantity must return one number.

    The delay landscape and this model both complex-sum the same banked pair
    with one branch inverted; read at the same shoulders with the same
    ``crossover_null_depth_db``, a disagreement means they have drifted into
    two different models of the same speaker.
    """

    freqs = _grid()
    woofer_tf = _lr4(freqs, highpass=False)
    tweeter_tf = _lr4(freqs, highpass=True)
    lower = _banked("woofer", woofer_tf, freqs)
    upper = _banked("tweeter", tweeter_tf, freqs)
    spec = sweep_spec(
        crossover_fc_hz=FC_HZ, upper_role="tweeter", lower_role="woofer",
        signed_acoustic_path_difference_m=0.0,
    )

    landscape_depth = delay_landscape.predicted_null_depth_db(
        lower, upper,
        crossover_fc_hz=FC_HZ,
        relative_delay_us=relative_delay_us,
        inverted_role=spec.positive_delay_target,
        lower_role=spec.negative_delay_target,
        upper_role=spec.positive_delay_target,
    )
    span = delay_landscape.curve_shoulder_span(
        lower, upper, crossover_fc_hz=FC_HZ,
        lower_role=spec.negative_delay_target,
        upper_role=spec.positive_delay_target,
    )
    predicted = predict_sum(
        _pair(woofer_tf, tweeter_tf, freqs),
        SummationCandidate(polarity_sign=-1, residual_delay_us=relative_delay_us),
    )
    model_depth = crossover_null_depth_db(
        predicted.freqs_hz, predicted.predicted_db, FC_HZ,
        shoulders_hz=span.used_hz,
    )

    assert model_depth == pytest.approx(landscape_depth, abs=1e-9)


# --------------------------------------------------------------------------- #
# the loader
# --------------------------------------------------------------------------- #


def test_the_loader_reads_both_solos_from_the_bank_the_store_wrote(
    tmp_path,
) -> None:
    """Magnitude AND phase reconstruct the transfer exactly (ruling R9), read
    through the shared index and the shared complex parse — never from a WAV."""

    freqs = _grid()
    woofer_tf = _lr4(freqs, highpass=False)
    tweeter_tf = _lr4(freqs, highpass=True) * np.exp(
        -2j * np.pi * freqs * 200.0 * 1e-6
    )
    bundle = _bank_take(tmp_path, [
        _banked("woofer", woofer_tf, freqs),
        _banked("tweeter", tweeter_tf, freqs),
    ])

    pair = load_branch_pair(bundle, phase=PHASE_MEASURE, position_deg=0)

    assert pair is not None
    assert pair.take_path.endswith("positions/p0_a01.json")
    assert np.allclose(pair.freqs_hz, freqs)
    assert np.allclose(pair.woofer_tf, woofer_tf)
    assert np.allclose(pair.tweeter_tf, tweeter_tf)
    assert pair.band_hz_by_role == {"woofer": BAND, "tweeter": BAND}


@pytest.mark.parametrize(
    "curves",
    [
        pytest.param([], id="no_curves"),
        pytest.param(["woofer"], id="one_solo_only"),
    ],
)
def test_the_loader_has_no_pair_when_a_take_lacks_a_solo(tmp_path, curves) -> None:
    """A round that measured one driver is an ordinary shape, never an error."""

    freqs = _grid()
    bundle = _bank_take(tmp_path, [
        _banked(role, _lr4(freqs, highpass=False), freqs) for role in curves
    ] or [{"role": "woofer"}])

    assert load_branch_pair(bundle, phase=PHASE_MEASURE, position_deg=0) is None


def test_curves_on_disagreeing_grids_refuse_rather_than_summing_across_them(
    tmp_path,
) -> None:
    """Two grids of equal LENGTH over different abscissae are the dangerous
    case: the sum would add bins that are not the same frequency and the result
    would still look like a spectrum. That is a defect, not an absence."""

    freqs = _grid(256)
    shifted = _grid(256, band=(220.0, 12500.0))
    bundle = _bank_take(tmp_path, [
        _banked("woofer", _lr4(freqs, highpass=False), freqs),
        _banked("tweeter", _lr4(shifted, highpass=True), shifted),
    ])

    with pytest.raises(ForwardModelError) as excinfo:
        load_branch_pair(bundle, phase=PHASE_MEASURE, position_deg=0)
    assert excinfo.value.refusal_reason == REFUSAL_GRID_DISAGREES


def test_the_shared_complex_parse_inverts_the_banked_serialization() -> None:
    """The one place a banked curve becomes a transfer function, and the exact
    inverse of ``pose_curve_record``'s ``magnitude_db`` / ``phase_deg`` pair."""

    freqs = _grid(128)
    tf = _lr4(freqs, highpass=True) * np.exp(-2j * np.pi * freqs * 90.0 * 1e-6)

    parsed = parse_curve_complex(_banked("tweeter", tf, freqs))

    assert parsed is not None
    grid, transfer, band = parsed
    assert np.allclose(grid, freqs)
    assert np.allclose(transfer, tf)
    assert band == BAND


@pytest.mark.parametrize(
    "curve",
    [
        pytest.param({"role": "w"}, id="missing_arrays"),
        pytest.param(
            {"role": "w", "freqs_hz": [1.0, 2.0], "magnitude_db": [0.0, 0.0]},
            id="no_phase",
        ),
        pytest.param(
            {"role": "w", "freqs_hz": [1.0, 2.0], "magnitude_db": [0.0, 0.0],
             "phase_deg": [0.0]},
            id="ragged_phase",
        ),
    ],
)
def test_the_shared_complex_parse_declines_a_curve_it_cannot_reconstruct(
    curve,
) -> None:
    assert parse_curve_complex(curve) is None


# --------------------------------------------------------------------------- #
# predicted vs measured
# --------------------------------------------------------------------------- #


def test_a_pure_level_difference_is_reported_as_offset_not_as_shape_error() -> None:
    """A forward model over banked solos carries no absolute SPL reference, so
    the raw offset against a measured sum is a LEVEL difference. It is removed
    before subtracting and published as the fact it removed."""

    freqs = _grid()
    predicted = predict_sum(_flat_pair(freqs), SummationCandidate())
    offset_db = 7.5

    delta = predicted_minus_measured_db(
        predicted, freqs, predicted.predicted_db - offset_db
    )

    assert delta["level_offset_db"] == pytest.approx(offset_db)
    assert delta["max_abs_db"] == pytest.approx(0.0, abs=1e-9)
    assert delta["rms_db"] == pytest.approx(0.0, abs=1e-9)
    assert delta["compared_band_hz"] == [BAND[0], BAND[1]]
    assert delta["compared_points"] == freqs.size
    assert delta["take_path"] == predicted.take_path


def test_a_shape_difference_survives_the_level_normalisation() -> None:
    """The delta reports SHAPE: a single-bin bump on the measured curve comes
    back at its own size, on the bin it was put on."""

    freqs = _grid()
    predicted = predict_sum(_flat_pair(freqs), SummationCandidate())
    measured = predicted.predicted_db.copy()
    measured[100] -= 3.0

    delta = predicted_minus_measured_db(predicted, freqs, measured)

    assert delta["max_abs_db"] == pytest.approx(3.0)
    assert delta["delta_db"][100] == pytest.approx(3.0)
    assert delta["freqs_hz"][100] == pytest.approx(float(freqs[100]))


def test_a_measured_curve_that_is_not_a_curve_refuses() -> None:
    freqs = _grid()
    predicted = predict_sum(_flat_pair(freqs), SummationCandidate())

    with pytest.raises(ForwardModelError):
        predicted_minus_measured_db(predicted, freqs, freqs[:-1])


# --------------------------------------------------------------------------- #
# the operator door
# --------------------------------------------------------------------------- #


def _record(round_dir: Path) -> dict:
    """The view's own artifact, read from where it files it beside the round."""

    return json.loads((round_dir / "forward_model.json").read_text())


def test_the_door_predicts_from_the_bank_and_says_nothing_judged_it(
    tmp_path,
) -> None:
    """The advisory verb sums the round's banked solos and files the curve.

    Issue #3481: it used to emit identically authoritative JSON whether or not
    anything had ever checked the model against a measurement, and three
    campaign decisions were triaged on it before the acceptance gate — which
    lived only in ``--help`` prose — reached the driver. The record now carries
    that nothing judged this one.
    """
    basis = bank_measure_round(tmp_path)

    code = cli_main(["forward-model", str(basis), "--residual-delay-us", "100"])
    payload = _record(basis)

    assert code == 0
    prediction = payload["prediction"]
    assert prediction["take_path"].endswith("positions/measure_02_a01.json")
    assert len(prediction["predicted_db"]) == len(prediction["freqs_hz"]) > 0
    assert prediction["sum_band_hz"] == [SOLO_BAND_HZ[0], SOLO_BAND_HZ[1]]
    # WHAT was predicted, on the filed record: the flag overrode the candidate,
    # and a curve that cannot be attributed to a chain has no provenance.
    assert payload["candidate"]["residual_delay_us"] == 100.0
    assert payload["predicted_minus_measured"] is None
    assert payload["acceptance"] == {
        "status": ACCEPTANCE_NOT_RUN,
        "judged_against": None,
    }


def test_the_acceptance_runs_worked_example_runs_on_what_the_flow_banks(
    tmp_path,
) -> None:
    """Issue #3482: ``ACCEPTANCE_RUNS`` run 1 must be runnable.

    It is the model's entry gate and it describes a two-round operation —
    predict from the round that banked the SOLOS, delta against the round that
    MEASURED the verify sum — while the verb once took one round for both
    halves. No banked round has both: stage 1 walks the solos and stage 2 opens
    a new bundle for the verify. Driven through ``main`` with the flags the help
    prescribes, over the two round shapes the shared real-shape builder banks,
    and the comparand is named on the record rather than left in an invocation
    the reader no longer has (#3481).
    """
    basis = bank_measure_round(tmp_path)
    measured = bank_verify_round(tmp_path)

    code = cli_main([
        "forward-model", str(basis), "--measured-round", str(measured),
        "--residual-delay-us", "-100",
    ])
    payload = _record(basis)

    assert code == 0
    assert payload["basis_round_dir"] == str(basis)
    assert payload["measured_round_dir"] == str(measured)
    assert payload["predicted_minus_measured"]["compared_points"] > 0
    assert payload["acceptance"] == {
        "status": ACCEPTANCE_JUDGED,
        "judged_against": str(measured),
    }


def test_every_flag_the_acceptance_runs_prescribe_is_a_flag_the_parser_has(
) -> None:
    """The worked example and the parser cannot drift apart again.

    ``ACCEPTANCE_RUNS`` is the model's entry gate, and it shipped naming an
    invocation the tool could not execute. Structural, not prose: the option
    tokens are lifted out of the acceptance text and matched against the
    parser's own option strings, so the pin holds however the sentences around
    them are reworded.
    """
    parser = build_parser()
    options = {
        string
        for action in parser._subparsers._group_actions[0].choices["forward-model"]._actions
        for string in action.option_strings
    }
    prescribed = set(re.findall(r"--[a-z][a-z0-9-]*", ACCEPTANCE_RUNS))

    assert prescribed
    assert prescribed <= options


def test_a_delta_that_was_asked_for_and_could_not_be_made_refuses(
    tmp_path, capsys,
) -> None:
    """Naming ``--measured-round`` is asking to be judged, so a round that banks
    no VERIFY sum answers the question asked with a refusal — not with an
    unjudged prediction filed under exit 0, which would read as the answer."""

    basis = bank_measure_round(tmp_path)

    code = cli_main([
        "forward-model", str(basis), "--measured-round", str(basis),
    ])
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_REFUSED
    assert payload["reason"] == REASON_REFUSED
    assert not (basis / "forward_model.json").exists()


def test_a_bank_that_cannot_answer_refuses_as_an_output(tmp_path, capsys) -> None:
    """A refusal is a payload with a reason, not a traceback — a round with no
    take carrying both solos is a finding about the bank, and no forward model
    at all rather than an unjudged one."""

    basis = bank_verify_round(tmp_path)

    code = cli_main(["forward-model", str(basis)])
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_REFUSED
    assert payload["status"] == "refused"
    assert payload["reason"] == REASON_REFUSED
