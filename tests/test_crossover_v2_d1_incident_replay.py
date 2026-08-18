# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free replay of the 2026-08-17 jts3 series-2 rollback (series-2 D1).

**The round that came off, and why it should not have.** Round 2 of series 2
measured pooled flatness 0.6711 dB — the flattest result this program has
produced — and the adoption table restored it under
``row3_unsafe``/``boost_realized_above_declared_bound``, reporting
``boost_overshoot_db = 3.948``. That number is not delivered energy. By
algebraic identity in the shipped code the rule measured

    realized − commanded  ≡  (measured − predicted) − expected_offset

— the commanded term is added by the caller and subtracted by the rule, so it
cancels and the quantity is the acoustic model's own error. The round had
``probe_verdict = matched``, ``gain_factor = 0.8469`` (it *under*-realized its
commanded slope), ``residual_offset_db = −0.064``, and a speaker measured
2.42 dB QUIETER than the round before. What tripped was a +3.9 dB model error at
1384 Hz **that both rounds of the series shared**, in a band the applied graph
declares a 3.67 dB CUT in, admitted to the boosted mask only because the
round-over-round command at that bin happened to flip −0.49 → +0.86 dB.

**The curves are the real ones.** ``tests/fixtures/crossover_v2_d1_incident_
20260817/verify_curves.json`` holds both rounds' persisted 511-bin transport
grid — measured, predicted, commanded, declared, entry baseline — lifted from
``captures/xover-series2-2026-08-17/series2-state-{r1b,r2}.json``. The shipped
probe ran 155,018 bins, so scalars reproduce to ~0.06 dB and the structured-run
booleans do not reproduce at all (r2's boosted run measures 0.249 octaves here
against the 1/3-octave bar it cleared on the full grid). Every assertion below
is therefore on a QUANTITY or on a decision that quantity drives, never on the
decimated width.

**What this file pins, in both directions.** That the repaired rule reads the
same curves as no finding and lets r2 take the row it earned; that the SAME
curves with real undeclared energy added still refuse; and that the model error
the old rule was reading is still measured, still on the record, and now lands
on the axis that says what the next round should fix.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.contracts import (
    EvidenceTrust,
    IterationHeadroom,
    QualityStatus,
    SafetyStatus,
)
from jasper.active_speaker.crossover_v2.verification import (
    ADOPTION_ROW_RESTORE_UNSAFE,
    ADOPTION_UNPROVEN,
    HEADROOM_REACHABLE,
    QUALITY_MODEL_DEPARTURE,
    SAFETY_BOOST_OVER_DECLARED_BOUND,
    TRUST_MEASURED,
    Verdict,
    decide_adoption,
    evaluate_applied_safety,
)
from jasper.active_speaker.delta_probe import (
    DELTA_PROBE_TOLERANCE_LOW_DB,
    VERDICT_MATCHED,
    boost_overshoot,
    classify_delta_probe,
    graded_command_floor_db,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "crossover_v2_d1_incident_20260817"
    / "verify_curves.json"
)

#: The band the shipped round graded, off its own journal line
#: (``event=correction.crossover_v2_delta_probe trusted_band_hz``).
BAND_HZ = (357.1, 20_000.0)

#: Where the standing model error peaks, and where the shipped rule reported its
#: overshoot. Half an octave BELOW the dip r2 actually closed (1947.2 Hz), which
#: is one of the three arguments in the diagnosis that the closure was real.
OVERSHOOT_HZ = 1384.1

#: The blend region the applied graph names (``round_measurements.blend.band_hz``)
#: — where the two-branch model is known blind (#2600) and where the standing
#: error lives.
BLEND_BAND_HZ = (824.35, 3297.4)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _round(tag: str) -> dict:
    """One round's curves, on one grid, with NaN restored for excluded bins."""
    data = _fixture()["rounds"][tag]

    def arr(key):
        return np.asarray(
            [np.nan if v is None else v for v in data[key]], dtype=float
        )

    freqs = arr("freqs_hz")
    measured = arr("measured_db")
    predicted = arr("predicted_db")
    commanded = arr("commanded_delta_db")
    return {
        "freqs": freqs,
        "measured": measured,
        "predicted": predicted,
        "commanded": commanded,
        "declared": arr("declared_transfer_db"),
        # The two curves the flow reconstructs, by the flow's own arithmetic
        # (``crossover_v2_flow._run_delta_probe`` / ``._entry_delta_db``).
        "realized": (measured - predicted) + commanded,
        "entry": (arr("entry_baseline_db") - predicted) + commanded,
        "offset": float(data["expected_offset_db"]),
        "shipped": data["shipped_receipt"],
    }


def _probe(tag: str, *, measured_delta_db=None):
    """The round, classified exactly as ``_run_delta_probe`` classifies it.

    ``measured_delta_db`` adds dB to the POST-apply capture only — the one place
    real undeclared energy would show up — leaving the entry capture, the model
    and every commanded curve untouched.
    """
    r = _round(tag)
    realized = r["realized"]
    if measured_delta_db is not None:
        realized = realized + measured_delta_db
    return classify_delta_probe(
        r["freqs"], realized, r["commanded"],
        band_hz=BAND_HZ,
        expected_offset_db=r["offset"],
        entry_delta_db=r["entry"],
        declared_transfer_db=r["declared"],
    )


def _adoption(safety):
    """The other three axes exactly as the round's own ``round_graded`` line.

    ``spec=failed trust=trusted safety=? quality=missed headroom=reachable`` —
    both rounds, verbatim. Only the safety axis differs between them, which is
    the diagnosis's central claim and what makes this a one-variable test.
    """
    return decide_adoption(
        trust=Verdict(EvidenceTrust.TRUSTED, TRUST_MEASURED, {}),
        safety=safety,
        quality=Verdict(QualityStatus.MISSED, ADOPTION_UNPROVEN, {}),
        headroom=Verdict(IterationHeadroom.REACHABLE, HEADROOM_REACHABLE, {}),
        boosted=True,
        rollback_available=True,
    )


def _at(freqs: np.ndarray, hz: float) -> int:
    return int(np.argmin(np.abs(freqs - hz)))


# --------------------------------------------------------------------------- #
# the fixture is the incident
# --------------------------------------------------------------------------- #


def test_the_banked_curves_reproduce_the_shipped_receipt():
    """Fidelity first: these really are the round the receipt describes.

    The quantity the shipped rule reported is the unanchored one, which this
    module still measures and now calls ``max_signed_error_db``. Reproducing it
    on the decimated grid is what earns every other assertion in this file —
    without it they would be claims about a synthetic curve that resembles the
    incident.
    """
    for tag in ("r1b", "r2"):
        probe = _probe(tag)
        shipped = _round(tag)["shipped"]
        assert probe.verdict == shipped["probe_verdict"], tag
        # The SAFETY-mask maximum, which is what the shipped receipt's
        # ``max_signed_error_db`` was and what this module still measures under
        # that name. (The shipped ``boost_overshoot_db`` was the same quantity
        # over the narrower boosted mask — identical to 16 digits in r2, 0.49 dB
        # lower in r1b, where the peak bin was not admitted.)
        assert probe.max_signed_error_db == pytest.approx(
            shipped["max_signed_error_db"], abs=0.06,
        ), tag
        assert probe.residual_offset_db == pytest.approx(
            shipped["residual_offset_db"], abs=0.05,
        ), tag

    # ...and r2's headline sat where the diagnosis says it sat.
    r2 = _probe("r2")
    assert r2.max_signed_error_db == pytest.approx(3.891, abs=5e-3)
    assert r2.gain_factor == pytest.approx(0.847, abs=5e-3)


def test_the_standing_model_error_belongs_to_the_comparison_not_to_either_round():
    """The diagnosis's central measurement, recomputed from the banked curves.

    If the +3.9 dB feature were something round 2 DID, it would not be in round
    1b's capture — and it is, within 0.35 dB rms across the blend region. This
    is the fact that makes the pre-fix rule's answer a statement about our
    model rather than about the speaker, and it is measured here rather than
    quoted.
    """
    curves = []
    for tag in ("r1b", "r2"):
        r = _round(tag)
        freqs = r["freqs"]
        # Band-referenced, exactly as the diagnosis reports it: the model error
        # minus its own mean over the spec reference band, so a whole-band level
        # difference between the two rounds cannot manufacture agreement.
        error = r["measured"] - r["predicted"]
        ref = (freqs >= 357.14) & (freqs <= 8000.0) & np.isfinite(error)
        curves.append(error - float(np.mean(error[ref])))

    freqs = _round("r1b")["freqs"]
    blend = (
        (freqs >= BLEND_BAND_HZ[0])
        & (freqs <= BLEND_BAND_HZ[1])
        & np.isfinite(curves[0])
        & np.isfinite(curves[1])
    )
    a, b = curves[0][blend], curves[1][blend]
    assert float(np.corrcoef(a, b)[0, 1]) == pytest.approx(0.954, abs=0.02)
    assert float(np.sqrt(np.mean((a - b) ** 2))) == pytest.approx(0.350, abs=0.05)

    # The peak is in BOTH rounds, at the same bin, at nearly the same size.
    i = _at(freqs, OVERSHOOT_HZ)
    assert curves[0][i] == pytest.approx(3.46, abs=0.05)
    assert curves[1][i] == pytest.approx(3.93, abs=0.05)


def test_the_only_thing_that_changed_at_that_bin_was_which_way_the_command_moved():
    """The mask flip, measured — and the graph is CUTTING there in both rounds.

    The rule's docstring said it detects "energy in a driver the graph did not
    declare". At the bin it fired on, the graph declares 3.67 dB LESS energy
    than the raw crossover, and did in the round before too. What differed is a
    0.86 dB relaxation of a cut, which is what put the bin inside a mask keyed
    on ``commanded > 0 | declared > 0``.
    """
    for tag, commanded_db, declared_db, boosted in (
        ("r1b", -0.486, -4.696, False),
        ("r2", +0.859, -3.666, True),
    ):
        r = _round(tag)
        i = _at(r["freqs"], OVERSHOOT_HZ)
        assert r["commanded"][i] == pytest.approx(commanded_db, abs=0.01), tag
        assert r["declared"][i] == pytest.approx(declared_db, abs=0.01), tag

        floor = graded_command_floor_db(r["freqs"])
        in_band = (r["freqs"] >= BAND_HZ[0]) & (r["freqs"] <= BAND_HZ[1])
        graded = in_band & (np.abs(r["commanded"]) >= floor)
        safety = graded | (in_band & (np.abs(r["declared"]) >= floor))
        mask = safety & ((r["commanded"] > 0.0) | (r["declared"] > 0.0))
        assert bool(mask[i]) is boosted, tag


# --------------------------------------------------------------------------- #
# the repair, both directions
# --------------------------------------------------------------------------- #


def test_the_pre_fix_quantity_trips_and_the_repaired_one_does_not():
    """The whole defect and the whole fix, on one line each, same curves.

    Both readings go through the shipped rule; only the excess curve differs.
    The unanchored one is what the code computed before D1 — model error, +3.89
    at 1384 Hz, over the 1.5 dB tolerance. The anchored one differences the
    pre-apply capture per bin, which cancels a feature present in both captures
    and leaves +1.00 dB: under tolerance, no finding.
    """
    r = _round("r2")
    freqs, commanded, declared = r["freqs"], r["commanded"], r["declared"]
    floor = graded_command_floor_db(freqs)
    in_band = (freqs >= BAND_HZ[0]) & (freqs <= BAND_HZ[1])
    graded = in_band & (np.abs(commanded) >= floor)
    safety = graded | (in_band & (np.abs(declared) >= floor))

    unanchored = (r["realized"] - r["offset"]) - commanded
    anchored = unanchored - r["entry"]
    tolerance = np.full_like(freqs, DELTA_PROBE_TOLERANCE_LOW_DB)

    _over, worst_unanchored, _oct = boost_overshoot(
        freqs, unanchored, commanded, tolerance,
        safety & np.isfinite(unanchored), declared_db=declared,
    )
    _over, worst_anchored, _oct = boost_overshoot(
        freqs, anchored, commanded, tolerance,
        safety & np.isfinite(anchored), declared_db=declared,
    )

    assert worst_unanchored == pytest.approx(3.891, abs=5e-3)
    assert worst_unanchored > DELTA_PROBE_TOLERANCE_LOW_DB
    # The repaired reading at the same bin, and the same bin is no longer the
    # worst one — the feature it was reading is gone from the curve entirely.
    i = _at(freqs, OVERSHOOT_HZ)
    assert anchored[i] == pytest.approx(1.000, abs=5e-3)
    assert anchored[i] < DELTA_PROBE_TOLERANCE_LOW_DB
    assert worst_anchored == pytest.approx(2.082, abs=5e-3)
    # ...and what remains is a real measured HF change, above the 10 kHz split
    # where this probe concedes 2.5 dB. Disclosed, not refused on.
    assert freqs[_at(freqs, 16_914.9)] > 10_000.0


def test_round_2_grades_safe_and_takes_the_row_round_1b_took():
    """The payoff, through the shipped table.

    The two rounds' ``round_graded`` axis lines differed on exactly one axis:
    ``safety=safe`` for r1b, ``safety=unsafe`` for r2. Everything else — spec
    failed, trust trusted, quality missed, headroom reachable — was identical.
    So flipping the one axis the diagnosis shows was measuring the wrong
    quantity has to put r2 on r1b's row, and it does.
    """
    rows = {}
    for tag in ("r1b", "r2"):
        probe = _probe(tag)
        safety = evaluate_applied_safety(probe=probe, integrity=None)
        assert safety.status is SafetyStatus.SAFE, tag
        assert safety.evidence["safety_anchored"] is True, tag
        assert probe.boost_over_declared_bound is False, tag
        assert probe.realized_louder_than_commanded is False, tag
        rows[tag] = _adoption(safety)

    assert rows["r2"].row == rows["r1b"].row == "row2_trusted_safe_missed"
    assert rows["r2"].outcome.value == "keep_for_iteration"
    # The receipt the shipped round actually wrote, for contrast.
    assert _round("r2")["shipped"]["adoption_row"] == ADOPTION_ROW_RESTORE_UNSAFE


def test_the_same_round_with_real_undeclared_energy_still_comes_off():
    """The fail-closed control, and it is the same curves plus one real change.

    +4 dB added to the POST-apply capture from 4 kHz up — energy the speaker
    delivered and the graph never declared, with the model, the entry capture
    and every commanded curve untouched. A rule that answered "no finding"
    above has to answer "restore" here, or it is not a safety rule; and the
    amount it reports has to be the round's own worst anchored excess PLUS the
    4 dB exactly, not that plus a model error it has confused for energy.
    """
    freqs = _round("r2")["freqs"]
    hot = np.where(freqs >= 4_000.0, 4.0, 0.0)
    probe = _probe("r2", measured_delta_db=hot)

    assert probe.safety_anchored is True
    assert probe.boost_over_declared_bound is True
    assert probe.boost_overshoot_db == pytest.approx(2.082 + 4.0, abs=5e-3)
    assert probe.boost_overshoot_octaves >= 1.0
    assert probe.realized_louder_than_commanded is True

    safety = evaluate_applied_safety(probe=probe, integrity=None)
    assert safety.status is SafetyStatus.UNSAFE
    assert safety.reason == SAFETY_BOOST_OVER_DECLARED_BOUND
    decision = _adoption(safety)
    assert decision.row == ADOPTION_ROW_RESTORE_UNSAFE
    assert decision.outcome.value == "restore"

    # The other hazard door agrees, which is what a genuine level rise looks
    # like: the quiet bins moved too (r2's own quiet core is 5.5-6.8 kHz), so
    # the residual reads +3.07 dB against its 1.5 dB tolerance. The reason
    # string names the boost door because that one is checked first.
    assert probe.residual_offset_db == pytest.approx(3.066, abs=5e-3)

    # The control that makes the pair a measurement: the ONLY difference between
    # this and the round that keeps is 4 dB in the post-apply capture.
    assert _probe("r2").boost_over_declared_bound is False


def test_the_model_error_is_still_measured_and_reaches_the_next_round():
    """Nothing was deleted — it moved to the axis that says what to fix.

    The +3.9 dB blend-region departure is a real defect: the two-branch model
    places its crossover summation loss near 1400 Hz where this speaker's is
    near 1650. Keeping it as a hard stop cost the series its best round; losing
    it entirely would cost the next round the pointer.
    """
    from jasper.active_speaker.crossover_v2.contracts import SpecStatus
    from jasper.active_speaker.crossover_v2.verification import (
        RealizationStatus,
        BenefitStatus,
        evaluate_round_quality,
    )

    probe = _probe("r2")
    assert probe.verdict == VERDICT_MATCHED
    assert probe.model_departure_over_tolerance is True
    assert probe.max_signed_error_db == pytest.approx(3.891, abs=5e-3)

    quality = evaluate_round_quality(
        realization=Verdict(RealizationStatus.MATCHED, "tracked", {}),
        benefit=Verdict(BenefitStatus.INDETERMINATE, "unproven", {}),
        spec=Verdict(SpecStatus.FAILED, "band_out_of_tolerance", {}),
        probe=probe,
        spec_report=None,
    )
    # It is a TARGET, and a target moves no status.
    assert quality.status is QualityStatus.MISSED
    target = next(
        t for t in quality.evidence["targets"]
        if t.startswith(f"{QUALITY_MODEL_DEPARTURE}:")
    )
    assert "3.89dB" in target
    # The frequency the AMOUNT was measured at, which is not always the one
    # ``worst_hz`` names — see the next test. Here they coincide.
    assert probe.max_signed_error_hz == pytest.approx(OVERSHOOT_HZ, abs=1.0)
    assert f"@{OVERSHOOT_HZ:.0f}Hz" in target


def test_the_departures_own_frequency_is_not_the_worst_absolute_errors():
    """Two reductions over two bin sets, and r1b is where they part company.

    ``worst_hz`` is the worst ABSOLUTE error over the GRADED bins;
    ``max_signed_error_hz`` is the worst POSITIVE departure over the SAFETY
    bins. On this round they sit 563 Hz apart — far enough that a next-round
    target quoting the first frequency beside the second amount would point at
    a different acoustic feature entirely (1947 Hz is the dip r2 went on to
    close; 1384 Hz is the standing model error this whole file is about).
    """
    probe = _probe("r1b")
    assert probe.worst_hz == pytest.approx(1947.2, abs=1.0)
    assert probe.max_signed_error_hz == pytest.approx(1384.1, abs=1.0)
    assert probe.max_signed_error_db == pytest.approx(1.219, abs=5e-3)

    # ...and the pair travels on the record together.
    direction = probe.to_dict()["direction"]
    assert direction["max_signed_error_db"] == probe.max_signed_error_db
    assert direction["max_signed_error_hz"] == probe.max_signed_error_hz
