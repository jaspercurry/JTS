# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The offline attempts-loop replay driver.

Fixtures are **synthesized in this file**, not committed as JSON blobs and not
read from the real banks. Two reasons: the real repeat-floor bank is ~1 GB of
WAVs beside its analysis, and a fixture whose schema is visible three lines
above the assertion that depends on it is a fixture nobody has to go find.
:func:`_repeat_floor_bank` and :func:`_sessions_dir` mirror the exact shapes
the driver parses out of ``captures/repeat-floor-20260731/`` and
``captures/r11-loop-proof-corpus/``.

The one place real numbers appear is
:func:`test_percentile_reproduces_the_banked_studys_own_summary`, which is the
point: the claim floor is derived from those thirteen values and must not be a
decimal somebody typed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.active_speaker.attempts_loop import (
    CONTINUE,
    PROVENANCE_MODEL_GRADED,
    PROVENANCE_REALIZED,
    STOP_EVIDENCE,
    STOP_FLOOR,
    AttemptBudget,
)
from jasper.cli.active_speaker_attempts_replay import (
    METRIC_LINEARIZATION_RESIDUAL_RMS,
    METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
    ReplayError,
    build_repeat_floor_run,
    build_session_runs,
    main,
    percentile,
)

# The 13 accepted consecutive-pair deviations and the summary the banked
# analysis published beside them, from
# captures/repeat-floor-20260731/analysis/refined_A_product_level.json.
BANKED_CONSECUTIVE_PAIRS_DB = (
    0.08536762032995046, 0.05391299114504572, 0.06780682807511519,
    0.04345782979106651, 0.04950236738668734, 0.08488854887316596,
    0.059320252311060084, 0.03986753092202827, 0.07036603034748379,
    0.04779172921744562, 0.043052994786055565, 0.03582904705461224,
    0.05182642518225966,
)
BANKED_SUMMARY_MEDIAN_DB = 0.05183
BANKED_SUMMARY_P95_DB = 0.08508


# --------------------------------------------------------------------------
# Fixtures mirroring the banked schemas
# --------------------------------------------------------------------------


def _repeat_floor_bank(
    tmp_path: Path,
    *,
    deviations: tuple[float, ...] = (0.04, 0.05, 0.06, 0.045),
    rejected_repeat: int | None = 2,
) -> Path:
    """A miniature ``refined_A_product_level.json`` bank.

    ``deviations`` are consecutive-pair deviations; ``rejected_repeat`` is a
    capture the shipped gate threw out, which drops the two pairs that would
    have touched it — exactly what the real bank looks like around its one
    ``locate_failed`` capture.
    """

    # A rejected capture costs two pairs: the one that would have graded it,
    # and the one that would have graded against it.
    first_gradeable = (rejected_repeat + 2) if rejected_repeat else 2
    n_captures = first_gradeable + len(deviations) - 1
    verdicts = []
    for repeat in range(1, n_captures + 1):
        rejected = repeat == rejected_repeat
        verdicts.append({
            "repeat": repeat,
            "accepted": not rejected,
            "reasons": ["locate_failed", "agc_behavioral_fail"] if rejected else [],
            "sweep_locate_confidence": 0.075 if rejected else 0.78,
        })

    rows = [
        {
            "repeat": first_gradeable + index,
            "reference_repeat": first_gradeable + index - 1,
            "max_db_notch_excluded": value,
            "tracking_band_hz": [1000.0, 4000.0],
        }
        for index, value in enumerate(deviations)
    ]
    fixed_rows = [
        {
            "repeat": row["repeat"],
            "reference_repeat": 1,
            "max_db_notch_excluded": row["max_db_notch_excluded"] * 1.7,
        }
        for row in rows
    ]

    bank = tmp_path / "repeat-floor-fixture"
    analysis = bank / "analysis"
    analysis.mkdir(parents=True)
    (analysis / "refined_A_product_level.json").write_text(
        json.dumps({
            "kind": "jts_repeat_floor_refined",
            "block": "A_product_level",
            "n_captures": n_captures,
            "n_accepted_by_shipped_gate": n_captures - (1 if rejected_repeat else 0),
            "verdicts": verdicts,
            "shipped_comparator_accepted_pairs": {
                "vs_predecessor": {"n_pairs_accepted": len(rows), "rows": rows},
                "vs_repeat_1": {
                    "n_pairs_accepted": len(fixed_rows), "rows": fixed_rows,
                },
            },
        }),
        encoding="utf-8",
    )
    return bank


def _alternating_rejection_bank(tmp_path: Path, *, n_captures: int = 6) -> Path:
    """A bank whose gate rejects every other capture.

    Deviations are present for every repeat — so a floor still derives — but
    no two *comparable* captures ever land side by side, so not one comparison
    completes. This is the shape that made the run's gradeability check matter:
    the kernel still attaches a two-attempt basis to each refusal, and reading
    that as evidence let a bank with zero magnitudes exit 0.
    """

    verdicts = [
        {
            "repeat": repeat,
            "accepted": repeat % 2 == 1,
            "reasons": [] if repeat % 2 == 1 else ["locate_failed"],
            "sweep_locate_confidence": 0.78 if repeat % 2 == 1 else 0.075,
        }
        for repeat in range(1, n_captures + 1)
    ]
    rows = [
        {
            "repeat": repeat,
            "reference_repeat": repeat - 1,
            "max_db_notch_excluded": 0.05,
            "tracking_band_hz": [1000.0, 4000.0],
        }
        for repeat in range(2, n_captures + 1)
    ]
    bank = tmp_path / "alternating-fixture"
    analysis = bank / "analysis"
    analysis.mkdir(parents=True)
    (analysis / "refined_A_product_level.json").write_text(
        json.dumps({
            "kind": "jts_repeat_floor_refined",
            "n_captures": n_captures,
            "n_accepted_by_shipped_gate": sum(1 for v in verdicts if v["accepted"]),
            "verdicts": verdicts,
            "shipped_comparator_accepted_pairs": {
                "vs_predecessor": {"n_pairs_accepted": len(rows), "rows": rows},
            },
        }),
        encoding="utf-8",
    )
    return bank


def _session(
    root: Path,
    session_id: str,
    *,
    started_at: float,
    woofer_rms: float,
    tweeter_rms: float,
    build_sha: str = "abc123def",
    outcome: str = "fitted",
    tweeter_band: tuple[float, float] = (2019.9676592807216, 18390.878468068575),
) -> None:
    """One commissioning bundle: ``info.json`` plus a fitted ``candidate.json``."""

    session = root / session_id
    capture = session / "evidence/v1/artifacts/crossover_v2" / f"cap_{session_id}"
    capture.mkdir(parents=True)
    (session / "info.json").write_text(
        json.dumps({
            "kind": "jts_active_speaker_commissioning_bundle",
            "session_id": session_id,
            "started_at": started_at,
            "state": "abandoned",
            "fingerprints": {
                "build_sha": build_sha,
                "topology_fingerprint": "bb636f18" * 8,
            },
        }),
        encoding="utf-8",
    )
    (capture / "candidate.json").write_text(
        json.dumps({
            "kind": "jts_measured_crossover_candidate_v2",
            "linearization_outcome": outcome,
            "analysis": {
                "predicted_ripple_db": 9.0,
                "flatness_improvement_db": 1.0,
            },
            "linearization": {
                "woofer": {
                    "role": "woofer",
                    "residual_rms_db": woofer_rms,
                    "residual_max_db": woofer_rms * 4,
                    "fit_band_hz": [150.0, 2747.3373723751424],
                    "n_repeats": 2,
                },
                "tweeter": {
                    "role": "tweeter",
                    "residual_rms_db": tweeter_rms,
                    "residual_max_db": tweeter_rms * 4,
                    "fit_band_hz": list(tweeter_band),
                    "n_repeats": 2,
                },
            },
        }),
        encoding="utf-8",
    )


def _sessions_dir(tmp_path: Path, **overrides: object) -> Path:
    root = tmp_path / "sessions"
    root.mkdir()
    _session(root, "aaaa1111", started_at=1000.0, woofer_rms=3.0, tweeter_rms=3.4)
    _session(
        root, "bbbb2222", started_at=2000.0,
        woofer_rms=2.9, tweeter_rms=1.1,
        **overrides,  # type: ignore[arg-type]
    )
    return root


# --------------------------------------------------------------------------
# The floor decimal is mechanical
# --------------------------------------------------------------------------


def test_percentile_reproduces_the_banked_studys_own_summary():
    """The claim floor's inputs must be derivable, not transcribed.

    The banked analysis published median 0.05183 and p95 0.08508 for these
    thirteen pairs. If this driver's percentile disagreed, every floor it
    derives would silently differ from the study it cites.
    """

    assert percentile(BANKED_CONSECUTIVE_PAIRS_DB, 50.0) == pytest.approx(
        BANKED_SUMMARY_MEDIAN_DB, abs=5e-6,
    )
    assert percentile(BANKED_CONSECUTIVE_PAIRS_DB, 95.0) == pytest.approx(
        BANKED_SUMMARY_P95_DB, abs=5e-6,
    )
    # And therefore the claim floor, which is 2x that p95.
    assert 2 * percentile(BANKED_CONSECUTIVE_PAIRS_DB, 95.0) == pytest.approx(
        0.17016, abs=1e-5,
    )


def test_percentile_edges():
    assert percentile([4.0], 95.0) == 4.0
    assert percentile([1.0, 2.0, 3.0], 0.0) == 1.0
    assert percentile([1.0, 2.0, 3.0], 100.0) == 3.0
    assert percentile([1.0, 2.0, 3.0], 50.0) == 2.0
    with pytest.raises(ValueError):
        percentile([], 50.0)


# --------------------------------------------------------------------------
# Mode A — unchanged profile
# --------------------------------------------------------------------------


def test_repeat_floor_run_never_reads_a_change_on_an_unchanged_profile(tmp_path):
    run = build_repeat_floor_run(
        _repeat_floor_bank(tmp_path), budget=AttemptBudget(),
    )
    assert run.floor.metric == METRIC_VERIFY_MAX_NOTCH_EXCLUDED
    assert run.asserts_no_detectable_change is True
    assert run.contradictions == []
    assert run.improvement_claims == []
    assert run.gradeable is True
    # Every attempt whose predecessor was gradeable stops at the floor.
    assert [d.decision for d in run.decisions].count(STOP_FLOOR) == 4


def test_repeat_floor_run_carries_the_realized_provenance_label(tmp_path):
    run = build_repeat_floor_run(
        _repeat_floor_bank(tmp_path), budget=AttemptBudget(),
    )
    assert {a.provenance for a in run.attempts} == {PROVENANCE_REALIZED}
    # Decisions that actually compared, not ones that merely named a pair
    # before refusing it — a two-id basis rides on refusals too.
    graded = [d for d in run.decisions if d.magnitude_db is not None]
    assert graded and all(d.provenance == PROVENANCE_REALIZED for d in graded)


def test_a_gate_rejected_capture_is_never_graded(tmp_path):
    run = build_repeat_floor_run(
        _repeat_floor_bank(tmp_path, rejected_repeat=2), budget=AttemptBudget(),
    )
    by_id = dict(zip((a.attempt_id for a in run.attempts), run.decisions))
    assert by_id["r02"].decision == STOP_EVIDENCE
    assert by_id["r02"].reason == "attempt_not_comparable"
    assert by_id["r02"].magnitude_db is None
    # And the attempt after it is not graded across the gap either.
    assert by_id["r03"].decision == STOP_EVIDENCE
    assert by_id["r03"].reason == "predecessor_not_comparable"


def test_the_floor_is_derived_from_the_banks_own_consecutive_pairs(tmp_path):
    deviations = (0.10, 0.12, 0.11, 0.13)
    run = build_repeat_floor_run(
        _repeat_floor_bank(tmp_path, deviations=deviations),
        budget=AttemptBudget(),
    )
    assert run.floor.p95_db == pytest.approx(percentile(deviations, 95.0))
    assert run.floor.claim_floor_db == pytest.approx(
        2 * percentile(deviations, 95.0),
    )
    assert run.provenance["floor_is_derived_from_this_bank"] is True


def test_a_pair_spanning_a_gap_is_not_replayed_as_consecutive(tmp_path):
    """Only immediate-predecessor rows may become deltas."""

    bank = _repeat_floor_bank(tmp_path, rejected_repeat=None)
    path = bank / "analysis/refined_A_product_level.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["shipped_comparator_accepted_pairs"]["vs_predecessor"]["rows"].append(
        {"repeat": 3, "reference_repeat": 1, "max_db_notch_excluded": 99.0},
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    run = build_repeat_floor_run(bank, budget=AttemptBudget())
    magnitudes = [d.magnitude_db for d in run.decisions if d.magnitude_db]
    assert 99.0 not in magnitudes


def test_a_run_where_no_comparison_completed_is_not_gradeable(tmp_path):
    """Refusals name the pair they declined; that is not evidence of a grade."""

    run = build_repeat_floor_run(
        _alternating_rejection_bank(tmp_path), budget=AttemptBudget(),
    )
    assert all(d.magnitude_db is None for d in run.decisions)
    # Refusals still carry a two-attempt basis — the trap this pins.
    assert any(len(d.basis_attempt_ids) == 2 for d in run.decisions)
    assert run.gradeable is False


def test_main_exits_no_verdict_when_nothing_was_ever_compared(tmp_path, capsys):
    """Absence of evidence must not read as a pass."""

    code, report, _ = _run_main(
        tmp_path, "--repeat-floor", str(_alternating_rejection_bank(tmp_path)),
    )
    assert code == 2
    assert report["outcome"] == "no_verdict"
    assert report["ungradeable_runs"] == ["repeat-floor-unchanged-profile"]
    assert "no verdict" in capsys.readouterr().err


def test_repeat_floor_run_refuses_a_bank_it_cannot_read(tmp_path):
    empty = tmp_path / "empty"
    (empty / "analysis").mkdir(parents=True)
    (empty / "analysis/refined_A_product_level.json").write_text(
        json.dumps({"verdicts": [], "shipped_comparator_accepted_pairs": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ReplayError):
        build_repeat_floor_run(empty, budget=AttemptBudget())
    with pytest.raises(ReplayError):
        build_repeat_floor_run(tmp_path / "absent", budget=AttemptBudget())


# --------------------------------------------------------------------------
# Mode B — the session arc
# --------------------------------------------------------------------------


def test_session_runs_grade_each_role_separately(tmp_path):
    runs = build_session_runs(_sessions_dir(tmp_path), budget=AttemptBudget())
    by_id = {run.run_id: run for run in runs}
    assert set(by_id) == {"session-arc-woofer", "session-arc-tweeter"}

    woofer = by_id["session-arc-woofer"]
    tweeter = by_id["session-arc-tweeter"]
    assert woofer.floor.metric == METRIC_LINEARIZATION_RESIDUAL_RMS
    # 3.0 -> 2.9 is below the 0.5 dB policy bar; 3.4 -> 1.1 is well above it.
    assert woofer.decisions[-1].decision == STOP_FLOOR
    assert tweeter.decisions[-1].decision == CONTINUE
    assert tweeter.decisions[-1].improved is True
    assert tweeter.decisions[-1].improvement_db == pytest.approx(2.3)


def test_session_grades_are_labelled_model_graded(tmp_path):
    runs = build_session_runs(_sessions_dir(tmp_path), budget=AttemptBudget())
    for run in runs:
        assert {a.provenance for a in run.attempts} == {PROVENANCE_MODEL_GRADED}
        assert run.asserts_no_detectable_change is False
        assert "no_realized_grade" in run.provenance


def test_sessions_are_ordered_by_started_at_not_by_directory_name(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    # "zzzz" started first; alphabetical order would reverse the arc and turn
    # an improvement into a regression.
    _session(root, "zzzz9999", started_at=1000.0, woofer_rms=3.0, tweeter_rms=3.4)
    _session(root, "aaaa0000", started_at=2000.0, woofer_rms=2.9, tweeter_rms=1.1)
    runs = build_session_runs(root, budget=AttemptBudget())
    tweeter = next(r for r in runs if r.run_id.endswith("tweeter"))
    assert [a.attempt_id for a in tweeter.attempts] == ["zzzz9999", "aaaa0000"]
    assert tweeter.decisions[-1].improved is True


def test_a_moved_fit_band_makes_the_grades_incomparable(tmp_path):
    """A residual over a different span is a different number, same name."""

    root = _sessions_dir(tmp_path, tweeter_band=(3000.0, 18390.878468068575))
    runs = build_session_runs(root, budget=AttemptBudget())
    tweeter = next(r for r in runs if r.run_id.endswith("tweeter"))
    assert tweeter.attempts[-1].integrity.comparable is False
    assert any(
        "fit_band_changed" in reason
        for reason in tweeter.attempts[-1].integrity.reasons
    )
    assert tweeter.decisions[-1].decision == STOP_EVIDENCE
    # The woofer's band did not move, so its verdict is untouched.
    woofer = next(r for r in runs if r.run_id.endswith("woofer"))
    assert woofer.decisions[-1].decision == STOP_FLOOR


def test_a_refused_fit_is_not_graded(tmp_path):
    root = _sessions_dir(tmp_path, outcome="refused")
    runs = build_session_runs(root, budget=AttemptBudget())
    for run in runs:
        assert run.attempts[-1].integrity.comparable is False
        assert run.decisions[-1].decision == STOP_EVIDENCE


def test_a_bundle_with_two_candidates_is_refused_not_picked_from(tmp_path):
    """Nothing in a bundle orders its candidates, so a pick would be a coin flip."""

    root = _sessions_dir(tmp_path)
    second = (
        root / "aaaa1111/evidence/v1/artifacts/crossover_v2/cap_zzzz9999"
    )
    second.mkdir(parents=True)
    (second / "candidate.json").write_text(
        (root / "aaaa1111/evidence/v1/artifacts/crossover_v2/cap_aaaa1111"
         / "candidate.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(ReplayError) as excinfo:
        build_session_runs(root, budget=AttemptBudget())
    assert "undecidable" in str(excinfo.value)
    # Both capture ids are named, so the operator can disambiguate by hand.
    assert "cap_aaaa1111" in str(excinfo.value)
    assert "cap_zzzz9999" in str(excinfo.value)


def test_session_runs_refuse_an_arc_shorter_than_two(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    _session(root, "only1111", started_at=1.0, woofer_rms=3.0, tweeter_rms=3.4)
    with pytest.raises(ReplayError):
        build_session_runs(root, budget=AttemptBudget())


# --------------------------------------------------------------------------
# End to end through main()
# --------------------------------------------------------------------------


def _run_main(tmp_path: Path, *args: str) -> tuple[int, dict, str]:
    out = tmp_path / "out"
    code = main([*args, "--out", str(out)])
    report_path = out / "report.json"
    report = (
        json.loads(report_path.read_text(encoding="utf-8"))
        if report_path.exists() else {}
    )
    readme_path = out / "README.md"
    readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
    return code, report, readme


def test_main_replays_both_banks_and_exits_zero(tmp_path):
    code, report, readme = _run_main(
        tmp_path,
        "--repeat-floor", str(_repeat_floor_bank(tmp_path)),
        "--sessions", str(_sessions_dir(tmp_path)),
    )
    assert code == 0
    assert report["outcome"] == "consistent"
    assert [run["run_id"] for run in report["runs"]] == [
        "repeat-floor-unchanged-profile",
        "session-arc-woofer",
        "session-arc-tweeter",
    ]
    assert report["contradicted_runs"] == []
    # The report must be reproducible from what it prints.
    assert report["command"].startswith("jasper-active-speaker-attempts-replay")
    assert "## What ran" in readme
    assert "## Reproduce" in readme


def test_the_readme_states_the_provenance_of_every_delta(tmp_path):
    """A label only present in the JSON is a label the reader never sees."""

    _, _, readme = _run_main(
        tmp_path,
        "--repeat-floor", str(_repeat_floor_bank(tmp_path)),
        "--sessions", str(_sessions_dir(tmp_path)),
    )
    assert "**realized vs realized**" in readme
    assert "**model-graded vs model-graded**" in readme


def test_the_readme_shows_the_measured_case_against_a_fixed_baseline(tmp_path):
    _, _, readme = _run_main(
        tmp_path, "--repeat-floor", str(_repeat_floor_bank(tmp_path)),
    )
    assert "Why consecutive, not a fixed baseline" in readme
    assert "x larger" in readme


def test_main_reports_a_finding_when_an_unchanged_bank_shows_a_real_change(
    tmp_path,
):
    """The exit-1 path, reached by a bank that contradicts itself.

    Twenty quiet pairs set the p95, and one pair twenty times larger clears the
    floor those twenty imply. On a profile nobody touched, that is the loop
    reading a change that is not there — a finding, not a pass.

    The sample has to be this large for the finding to be *reachable at all*,
    and that is worth stating: because the floor is derived from the same
    pairs it grades, a small bank's outlier drags its own p95 up with it and
    can never contradict itself. At n = 13 (the real bank's size) the p95 sits
    at the 11.4th of 12 gaps — within a hair of the maximum — so 2 x p95 is
    always above it. **A self-derived floor is close to unfalsifiable on a
    small bank**, which is exactly why the generated report calls the
    repeat-floor run a self-consistency demonstration rather than a validation
    of the threshold.
    """

    bank = _repeat_floor_bank(
        tmp_path, deviations=(0.04,) * 20 + (0.90,), rejected_repeat=None,
    )
    code, report, _ = _run_main(tmp_path, "--repeat-floor", str(bank))
    assert code == 1
    assert report["outcome"] == "contradicted"
    assert report["contradicted_runs"] == ["repeat-floor-unchanged-profile"]
    assert report["runs"][0]["contradictions"]


def test_main_refuses_without_a_bank(tmp_path, capsys):
    assert main(["--out", str(tmp_path / "out")]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_main_refuses_an_unreadable_bank(tmp_path, capsys):
    code, report, _ = _run_main(tmp_path, "--repeat-floor", str(tmp_path / "gone"))
    assert code == 2
    assert report == {}
    assert "REFUSED" in capsys.readouterr().err


def test_main_refuses_an_impossible_budget(tmp_path, capsys):
    code, _, _ = _run_main(
        tmp_path,
        "--repeat-floor", str(_repeat_floor_bank(tmp_path)),
        "--target-attempts", "4",
        "--hard-cap-attempts", "2",
    )
    assert code == 2
    assert "REFUSED" in capsys.readouterr().err


def test_a_refusing_run_removes_a_previous_runs_verdict(tmp_path):
    """A stale report beside fresh outputs is a report someone will misread.

    The per-run stores go too: an orphaned ``*.model_error.json`` asserts an
    adopted claim floor, and a threshold with no report beside it is a number
    someone could pick up and believe.
    """

    out = tmp_path / "out"
    assert main([
        "--repeat-floor", str(_repeat_floor_bank(tmp_path)), "--out", str(out),
    ]) == 0
    assert (out / "report.json").exists()
    assert list(out.glob("*.model_error.json"))
    assert main(["--repeat-floor", str(tmp_path / "gone"), "--out", str(out)]) == 2
    assert not (out / "report.json").exists()
    assert not (out / "README.md").exists()
    assert list(out.glob("*.model_error.json")) == []


def test_each_run_banks_its_floor_into_its_own_store(tmp_path):
    """One store holds one speaker's one floor; these runs are not one speaker."""

    out = tmp_path / "out"
    assert main([
        "--repeat-floor", str(_repeat_floor_bank(tmp_path)),
        "--sessions", str(_sessions_dir(tmp_path)),
        "--out", str(out),
    ]) == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    stores = report["model_error_stores"]
    assert set(stores) == {
        "repeat-floor-unchanged-profile",
        "session-arc-woofer",
        "session-arc-tweeter",
    }
    assert stores["repeat-floor-unchanged-profile"]["floor"]["metric"] == (
        METRIC_VERIFY_MAX_NOTCH_EXCLUDED
    )
    assert stores["session-arc-woofer"]["floor"]["metric"] == (
        METRIC_LINEARIZATION_RESIDUAL_RMS
    )
    # No realized grade survived in either bank, so no model error can be
    # banked from them. An empty list here IS the rung-P4 finding.
    assert all(store["model_error"] == [] for store in stores.values())
    for run_id in stores:
        assert (out / f"{run_id}.model_error.json").exists()


def test_replay_never_writes_to_the_production_store_path(tmp_path, monkeypatch):
    """A replay must not adopt a floor onto a real speaker."""

    written: list[str] = []
    import jasper.active_speaker.model_error_store as store

    real_write = store._write_state

    def _spy(path, state):  # type: ignore[no-untyped-def]
        written.append(str(path))
        real_write(path, state)

    monkeypatch.setattr(store, "_write_state", _spy)
    out = tmp_path / "out"
    assert main([
        "--repeat-floor", str(_repeat_floor_bank(tmp_path)), "--out", str(out),
    ]) == 0
    assert written
    assert all(str(out) in path for path in written)
    assert not any("/var/lib" in path for path in written)
