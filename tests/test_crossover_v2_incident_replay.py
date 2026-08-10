# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free replay of the 2026-08-10 jts3 crossover incident (#2291).

**These tests pin behaviour that is WRONG.** They are characterization tests:
they describe what the prescription path does TODAY, defects included, so that
issue #2291 Phase 2 — which changes it — has to state which pinned number it is
moving and why. A green run here is not a claim that the speaker is correct; it
is a claim that the incident still reproduces. Phase 2 flips these assertions.

The incident, in one paragraph. A stage-1 Fc comparison ran on a session
configured at 2000 Hz, evaluated six corners, and recommended 1648.7 Hz. The
candidate it published for that corner carried a −13.013 dB tweeter trim, a
0.0 dB woofer trim, and the outcome string ``trim_rejected`` — and was applied.
Post-apply the speaker measured a failing absolute claim (5.456 dB over a
2.0 dB tolerance) and 7.727 dB of cloud flatness error over 250-2000 Hz against
a 1.5 dB tolerance. Two defects in the prescription path are visible in that
record and are what these tests pin:

1. ``_fit_linearization`` reads ``self._fc_hz`` — the SESSION's configured
   corner — at every Fc-driven site, while the candidate it is fitting arrives
   with ``candidate_sections`` at its OWN corner. Every non-configured
   candidate is therefore levelled and ripple-scanned at the wrong crossover.
2. ``trim_rejected`` names the outcome when the ripple scan drifts past
   ``LINEARIZATION_TRIM_SANITY_MARGIN_DB`` from the anchor — but the scan's
   trim is still COMMITTED whenever it levels better than the anchor. The
   string says rejected; the number that shipped is the rejected one.

The evidence is banked raw and SHA-verified under
``captures/jts3-incident-20260810-issue2291/`` (93 MB, gitignored). The small
JSON set these tests read is derived from it by
``scripts/derive-crossover-incident-fixture.py``, which has a ``--check`` mode.

**What replays exactly, and what does not.** Every scalar the decision path
consumes — the raw trim, both fits' give-back and level-frame offset, the
correction filters, the ripple scan's own result, the session and candidate
corners — is the incident's, so the anchor, the drift verdict, the outcome
string, the commit choice and the committed pair are all computed by
production from banked numbers and match the incident exactly.

The per-driver measured RESPONSES do not replay, and the reason is size, not
absence. They were never retained as arrays; re-deriving them offline from
``measure_program.wav`` plus the UMIK-2 calibration is possible and is what
``scripts/severed-twin-replay.py`` already does, but both inputs are
gitignored capture data and the analysis grid is far too large to commit — the
same session's VERIFY frame graded 37,080 bins across 1.7 kHz, so a full-band
complex response runs to ~5e5 bins per driver. The branches below are
therefore synthetic, and the two seams whose true output needs them —
``fit_driver_linearization`` and ``solve_ripple_optimal_trim`` — return the
incident's own recorded results instead.

One consequence is worth stating rather than leaving to be inferred: the
``difference_db`` values the commit decision turns on are computed by real code
over those synthetic zero-phase branches, so they are not the incident's own
level errors. What the incident's record does prove is their ORDERING — it
committed the scan's pair, so the scan levelled better — and the test asserts
that ordering as its own premise rather than assuming it holds.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.branch_chain import (
    CrossoverSection,
    crossover_response_db,
    radiating_band_hz,
)
from jasper.active_speaker.crossover_v2_flow import (
    LINEARIZATION_TRIM_SANITY_MARGIN_DB,
    CrossoverV2Conductor,
    V2FlowSeams,
    _rounded_band_hz,
)
from jasper.active_speaker.linearization_fit import LinearizationFilter, LinearizationFit
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    AlignmentEstimate,
    CrossoverCandidate,
    DriftEstimate,
    DriverResponse,
    ProgramAnalysis,
    SegmentLocation,
    predicted_branch_sum,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "crossover_v2_incident_20260810"
ROLES = ("woofer", "tweeter")
SESSION_ID = "cap_test_incident_20260810"
# Enough bins for compose_envelope's grid resampling to have something to work
# with; the same order the conductor's own linearizable fixtures use.
FREQS_HZ = np.linspace(100.0, 20000.0, 2048)


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


SESSION_CONTEXT = _fixture("session_context")
CANDIDATE_FIT = _fixture("candidate_fit")
EXPECTED_OUTCOME = _fixture("expected_outcome")

CONFIGURED_FC_HZ = SESSION_CONTEXT["configured_fc_hz"]
SELECTED_FC_HZ = SESSION_CONTEXT["selected_fc_hz"]
COMMITTED_DB = EXPECTED_OUTCOME["committed_attenuations_db"]
ANCHORED_DB = EXPECTED_OUTCOME["anchor_replay"]["anchored_trim_db"]


# --------------------------------------------------------------------------- #
# fixture -> production objects
# --------------------------------------------------------------------------- #


def _incident_fit(role: str) -> LinearizationFit:
    """Rebuild one role's ``LinearizationFit`` from the banked candidate.

    ``candidate.json``'s per-role linearization block IS the serialized fit, so
    this is deserialization, not reconstruction — the give-back and the
    level-frame offset the anchor is built from are the fit engine's own
    numbers from the incident, not numbers this test chose.
    """
    banked = dict(CANDIDATE_FIT["linearization"][role])
    return LinearizationFit(
        role=banked["role"],
        filters=tuple(
            LinearizationFilter(
                biquad_type=f["biquad_type"], freq=f["freq"], q=f["q"], gain=f["gain"],
            )
            for f in banked["filters"]
        ),
        fit_band_hz=tuple(banked["fit_band_hz"]),
        target_level_db=banked["target_level_db"],
        residual_rms_db=banked["residual_rms_db"],
        residual_max_db=banked["residual_max_db"],
        reason_summary=banked["reason_summary"],
        mic_tier=banked["mic_tier"],
        driver_class=banked["driver_class"],
        n_repeats=banked["n_repeats"],
        verify_band_hz=tuple(banked["verify_band_hz"]),
        verify_residual_rms_db=banked["verify_residual_rms_db"],
        verify_residual_max_db=banked["verify_residual_max_db"],
        observe_octave_summary=banked["observe_octave_summary"],
        hf_continuation_spend_db=banked["hf_continuation_spend_db"],
        hf_continuation_ceiling_hz=banked["hf_continuation_ceiling_hz"],
        hf_continuation_policy=banked["hf_continuation_policy"],
        hf_continuation_suppressed_reason=banked["hf_continuation_suppressed_reason"],
        measured_deficit_at_ceiling_db=banked["measured_deficit_at_ceiling_db"],
        correction_giveback_db=banked["correction_giveback_db"],
        headroom_cost_db=banked["headroom_cost_db"],
        level_frame_offset_db=banked["level_frame_offset_db"],
        lift_requested_db=banked["lift_requested_db"],
        lift_from_boost_db=banked["lift_from_boost_db"],
        lift_from_reduced_cuts_db=banked["lift_from_reduced_cuts_db"],
        lift_suppressed_reason=banked["lift_suppressed_reason"],
    )


def _session_preset() -> ActiveSpeakerPreset:
    """The preset the SESSION ran, rebuilt from the candidate's own copy.

    ``_evaluate_fc_candidate`` publishes each candidate with the session preset
    re-cornered at that candidate's Fc (id and ``fc_hz`` are the only fields it
    touches), so the banked candidate preset sits at 1648.7 Hz. Putting the
    corner back at the banked ``configured_fc_hz`` recovers the session's own.
    """
    preset = ActiveSpeakerPreset.from_mapping(CANDIDATE_FIT["source_preset"])
    return replace(preset, crossover_regions=tuple(
        replace(region, fc_hz=CONFIGURED_FC_HZ) for region in preset.crossover_regions
    ))


def _roles_bands() -> list[RoleBand]:
    bands = SESSION_CONTEXT["sweep_band_hz"]
    return [
        RoleBand("woofer", 0, FrequencyBand(*bands["woofer"])),
        RoleBand("tweeter", 1, FrequencyBand(*bands["tweeter"])),
    ]


def _branch_db(role: str) -> np.ndarray:
    """One synthetic measured branch, at the incident's own inter-driver level.

    Flat behind its own committed crossover shape, with the tweeter placed
    exactly ``|committed tweeter trim|`` above the woofer. That offset is read
    off the incident rather than tuned: the incident's record shows the ripple
    scan's trim WON the realized-level comparison against the anchor (the
    committed pair is the scan's, not the anchor's), and a tweeter that hot is
    what makes the scan's −13.013 dB the level-correct answer. The premise is
    asserted, not assumed — see ``test_..._commits_the_ripple_scan_trim``.

    Synthetic because the incident's own per-driver responses are too large to
    commit; see this module's docstring.
    """
    section = CrossoverSection(
        fc_hz=SELECTED_FC_HZ, order=CANDIDATE_FIT["crossover_region"]["order"],
        highpass=role == "tweeter",
    )
    level = abs(float(COMMITTED_DB["tweeter"])) if role == "tweeter" else 0.0
    return level + crossover_response_db(FREQS_HZ, (section,))


def _response(role: str) -> DriverResponse:
    magnitude_db = _branch_db(role)

    def one() -> DriverResponse:
        return DriverResponse(
            role=role, freqs_hz=FREQS_HZ, magnitude_db=magnitude_db,
            complex_tf=(10.0 ** (magnitude_db / 20.0)).astype(complex),
            gating={
                "applied": True,
                "window_ms": SESSION_CONTEXT["capture_context"]["gate_window_ms"],
                "floor_source": SESSION_CONTEXT["capture_context"]["gate_floor_source"],
            },
            snr=None,
            validity_floor_hz=SESSION_CONTEXT["capture_context"]["validity_floor_hz"],
        )

    # 1 primary + 2 repeats clears LINEARIZATION_MIN_PAIRED_OCCURRENCES, the
    # paired-N half of the fit's eligibility gate. The incident's own fits
    # record ``n_repeats`` 2.
    return replace(one(), repeat_responses=(one(), one()))


def _locate(segment_id: str) -> SegmentLocation:
    return SegmentLocation(
        segment_id=segment_id, kind="sweep", role=None, scheduled_start=0,
        located_start=0, residual_samples=0.0, confidence=0.9, peak_dbfs=-12.0,
        clipped=False,
    )


def _analysis(program_id: str) -> ProgramAnalysis:
    """A MEASURE analysis carrying the incident's own candidate scalars."""
    banked = CANDIDATE_FIT["analysis"]
    alignment = CANDIDATE_FIT["alignment"]
    inverted = banked["polarity"] == "inverted"
    responses = {role: _response(role) for role in ROLES}
    summed = predicted_branch_sum(
        responses["woofer"].complex_tf, responses["tweeter"].complex_tf,
        float(banked["trim_db"]["woofer"]), float(banked["trim_db"]["tweeter"]),
        -1 if inverted else 1,
    )
    return ProgramAnalysis(
        phase="measure",
        program_id=program_id,
        locations=tuple(
            _locate(seg) for seg in ("sweep_w", "sweep_t", "sweep_w_rep", "sweep_t_rep")
        ),
        drift=DriftEstimate(
            epsilon_ppm=SESSION_CONTEXT["capture_context"]["epsilon_ppm"],
            baselines_ppm={"woofer_repeat": 5.0},
            max_residual_samples=0.1,
            glitch_detected=False,
        ),
        mic_tier=SESSION_CONTEXT["mic_tier"],
        driver_responses=(responses["woofer"], responses["tweeter"]),
        alignment=AlignmentEstimate(
            delay_us=alignment["delay_us"], raw_delay_us=alignment["delay_us"],
            parallax_us=0.0, polarity=banked["polarity"],
            polarity_sign=-1 if inverted else 1, polarity_agrees_with_sum=True,
            confidence=SESSION_CONTEXT["capture_context"]["alignment_confidence"],
            status=ALIGNMENT_OK,
        ),
        candidate=CrossoverCandidate(
            trim_db=dict(banked["trim_db"]),
            trim_band_average_db=dict(banked["trim_band_average_db"]),
            polarity=banked["polarity"],
            delay_us=alignment["delay_us"],
            predicted_ripple_db=banked["predicted_ripple_db"],
            confidence=SESSION_CONTEXT["capture_context"]["alignment_confidence"],
        ),
        linearity_ok=True,
        predicted_sum=(
            FREQS_HZ, 20.0 * np.log10(np.maximum(np.abs(summed), 1e-12)),
        ),
        glitch_detected=False,
    )


def _conductor() -> CrossoverV2Conductor:
    """A conductor at the incident's CONFIGURED corner, with inert seams.

    Nothing here plays, captures, applies or publishes: the replay drives one
    method, and every seam exists only because the constructor wants one.
    """
    seams = V2FlowSeams(
        play=lambda phase, program: None,
        analyze=lambda *a, **k: None,
        publish_check=lambda plan, ambient: None,
        publish_candidate=lambda candidate: None,
        apply_complete=lambda: False,
        apply_failed=lambda: "",
    )
    return CrossoverV2Conductor(
        session_id=SESSION_ID,
        source_preset=_session_preset(),
        roles_bands=_roles_bands(),
        fc_hz=CONFIGURED_FC_HZ,
        driver_caps_dbfs={role: 0.0 for role in ROLES},
        session_volume_db=-20.0,
        seams=seams,
        driver_spacing_m=0.15,
        # The incident's own CHECK solve, so the MEASURE program the fit reads
        # its sweep bounds from is composed at construction — the same state a
        # session reaches by walking CHECK, without walking it.
        gain_plan_db=SESSION_CONTEXT["gain_plan_db"],
    )


class _Replay:
    """One drive of the current prescription path with the incident's numbers.

    Holds what the run observed: the Fc every Fc-driven seam was handed, both
    graded trim pairs with the level error production measured for each, and
    the candidate the fit produced.
    """

    def __init__(self) -> None:
        self.fc_seen: dict[str, list[float]] = {
            "overlap_band_hz": [], "solve_ripple_optimal_trim": [],
            "realized_branch_level_match": [],
        }
        self.graded: list[dict[str, float]] = []
        self.candidate: Any = None
        # What the fit was HANDED (the candidate's re-cornered sections) beside
        # what it would have read had it ignored them (the session's own), so
        # an R17 regression is a difference this replay can see.
        self.candidate_sections: dict[str, tuple[CrossoverSection, ...]] = {}
        self.configured_sections: dict[str, tuple[CrossoverSection, ...]] = {}
        self.fit_radiating_bands: dict[str, tuple[float, float]] = {}

    def _pair(self, *, scan: bool) -> dict[str, float]:
        """The graded pair that IS (or is not) the ripple scan's own trim.

        Identified by value rather than by call order: the scan's tweeter trim
        is the one injected below, so which pair is which never depends on the
        order production happens to grade them in.
        """
        scan_trim = float(COMMITTED_DB["tweeter"])
        hits = [
            pair for pair in self.graded
            if (pair["trim_t_db"] == scan_trim) is scan
        ]
        assert len(hits) == 1, f"expected one graded pair (scan={scan}), got {len(hits)}"
        return hits[0]


def _run_replay(monkeypatch: pytest.MonkeyPatch) -> _Replay:
    replay = _Replay()
    conductor = _conductor()

    real_overlap = flow.overlap_band_hz
    real_match = flow.realized_branch_level_match

    def spy_overlap(fc_hz, **kwargs):
        replay.fc_seen["overlap_band_hz"].append(float(fc_hz))
        return real_overlap(fc_hz, **kwargs)

    def spy_match(freqs, w_tf, t_tf, fc_hz, **kwargs):
        replay.fc_seen["realized_branch_level_match"].append(float(fc_hz))
        result = real_match(freqs, w_tf, t_tf, fc_hz, **kwargs)
        replay.graded.append({
            "trim_w_db": float(kwargs["trim_w_db"]),
            "trim_t_db": float(kwargs["trim_t_db"]),
            "difference_db": float(result.difference_db),
        })
        return result

    def fake_ripple(freqs, w_lin, t_lin, fc_hz, **kwargs):
        # The second of the two stubs (``fake_fit`` below is the other), and
        # like it, stubbed because its true inputs are the measured responses
        # this fixture cannot commit. It returns the incident's own scan
        # result, so everything the decision below does with it is the
        # incident's own arithmetic rather than this test's.
        replay.fc_seen["solve_ripple_optimal_trim"].append(float(fc_hz))
        return (
            float(COMMITTED_DB["tweeter"]),
            float(CANDIDATE_FIT["analysis"]["predicted_ripple_db"]),
            float(kwargs["seed_trim_db"]),
        )

    def fake_fit(resp, envelope, **kwargs):
        # The band the fit engine was actually bounded to. Recorded because it
        # is derived from ``sections``, which is where an R17 regression would
        # show: a fit that ignored ``candidate_sections`` would hand the engine
        # the SESSION's shape here while still accepting the kwarg.
        replay.fit_radiating_bands[resp.role] = tuple(kwargs["radiating_band_hz"])
        return _incident_fit(resp.role)

    monkeypatch.setattr(flow, "overlap_band_hz", spy_overlap)
    monkeypatch.setattr(flow, "realized_branch_level_match", spy_match)
    monkeypatch.setattr(flow, "solve_ripple_optimal_trim", fake_ripple)
    monkeypatch.setattr(flow, "fit_driver_linearization", fake_fit)

    replay.candidate_sections = conductor._fc_candidate_sections(SELECTED_FC_HZ)
    replay.configured_sections = {
        role: conductor._branch_crossover_sections(role) for role in ROLES
    }
    candidate_preset = replace(conductor._preset, crossover_regions=tuple(
        replace(region, fc_hz=SELECTED_FC_HZ)
        for region in conductor._preset.crossover_regions
    ))
    # ``_build_candidate``, with the exact keyword pair ``_evaluate_fc_candidate``
    # hands it for a non-configured corner — the seam where the prescription is
    # computed. Its caller ``_build_measure_candidate`` adds one further gate,
    # which grades the LINEARIZED predicted sum against the raw one; that gate
    # passed on the incident and is orthogonal to both defects, but it cannot
    # pass on synthetic branches without shaping them until it does, and a
    # fixture tuned to satisfy a gate is not evidence about anything.
    replay.candidate = conductor._build_candidate(
        _analysis(CANDIDATE_FIT["program_id"]), None,
        candidate_sections=replay.candidate_sections,
        source_preset=candidate_preset,
    )
    return replay


# --------------------------------------------------------------------------- #
# the banked record
# --------------------------------------------------------------------------- #


def test_the_fixture_is_the_incident_as_banked():
    """Guards the fixture itself: these are the numbers #2291 is about.

    Cheap, and it is what makes every assertion below readable as "the incident
    reproduces" rather than "some numbers agree". A fixture re-derived from a
    different session fails here before it can quietly move a pin.
    """
    fingerprint = "3df7a4da7f33f5dfaa55866334cfaf7ebdb32bfa76dd0405f41fcc8a79d0941d"
    assert CANDIDATE_FIT["fingerprint"] == fingerprint
    assert EXPECTED_OUTCOME["fingerprint"] == fingerprint
    assert EXPECTED_OUTCOME["applied"]["measured_candidate_fingerprint"] == fingerprint
    assert CONFIGURED_FC_HZ == 2000.0
    assert SELECTED_FC_HZ == 1648.7
    assert CANDIDATE_FIT["crossover_region"]["fc_hz"] == SELECTED_FC_HZ
    assert EXPECTED_OUTCOME["linearization_outcome"] == "trim_rejected"
    assert COMMITTED_DB == pytest.approx({"tweeter": -13.012979363787029, "woofer": 0.0})
    # The trim that shipped is the one the household then heard measured back:
    # a failing absolute claim and 7.727 dB of flatness error where 1.5 dB is
    # the tolerance. Banked verbatim — the retained curves are decimated for
    # display and cannot recompute these, so the verdicts travel as scalars.
    post_apply = EXPECTED_OUTCOME["post_apply"]
    assert post_apply["verify_claims"]["absolute"]["status"] == "fail"
    assert post_apply["cloud_flatness"]["passed"] is False
    assert post_apply["cloud_flatness"]["max_db"] > post_apply["cloud_flatness"][
        "tolerance_db"
    ]
    assert EXPECTED_OUTCOME["applied"]["corrections"]["tweeter"]["gain_db"] == pytest.approx(
        COMMITTED_DB["tweeter"]
    )


# --------------------------------------------------------------------------- #
# defect 1 — the fit reads the session's corner, not the candidate's
# --------------------------------------------------------------------------- #


def test_the_fit_reads_the_session_corner_while_the_candidate_carries_its_own(
    monkeypatch, caplog,
):
    """CURRENT behaviour, and it is the bug. #2291 Phase 2 changes it.

    The Fc-driven seams inside ``_fit_linearization`` — the overlap band, the
    ripple scan, and the realized-level match on BOTH candidate trim pairs —
    are handed ``self._fc_hz``, the corner the SESSION was configured at, while
    the same call arrives with ``candidate_sections`` at the corner actually
    being evaluated. On the incident that is 2000 Hz of levelling and scanning
    applied to a 1648.7 Hz candidate. The journal line that would have told an
    operator which corner the fit ran at reports the session's corner too.

    ``_fit_linearization`` reads ``self._fc_hz`` at six sites; this pins four
    of them (the overlap band at ``:12366``, the ripple scan at ``:12495``, the
    level match at ``:12811``, and the ``fit_band`` journal field at
    ``:12111``). **The other two are NOT pinned here and cannot be:** the
    straddle test at ``:12493`` that decides whether the ripple scan runs, and
    the ``fc_hz`` field at ``:12506`` of the ``ripple_trim_skipped`` event in
    that straddle's own else-branch. On this session's overlap band
    (1600-4000 Hz) both corners straddle identically, so the branch this replay
    takes is the same either way and the else-branch never runs — no assertion
    about THIS incident can tell them apart, which mutation confirms. Treat
    those two as covered by inspection, never by this test.

    Separately, this pins that the candidate's sections are USED and not merely
    accepted (R17) — see the radiating-band assertions at the end.

    After Phase 2 these seams should see the candidate's own corner; these
    assertions are expected to invert.
    """
    caplog.set_level("INFO", logger="jasper.active_speaker.crossover_v2_flow")
    replay = _run_replay(monkeypatch)

    for section in replay.candidate_sections.values():
        assert [s.fc_hz for s in section] == [SELECTED_FC_HZ] * len(section)
    assert replay.candidate.source_preset.crossover_regions[0].fc_hz == SELECTED_FC_HZ

    for seam, seen in replay.fc_seen.items():
        assert seen, f"{seam} was never reached — the replay did not exercise the fit"
        assert set(seen) == {CONFIGURED_FC_HZ}, (
            f"#2291: {seam} saw {sorted(set(seen))}; today it reads the session "
            f"corner {CONFIGURED_FC_HZ}, not the candidate's {SELECTED_FC_HZ}"
        )
        assert SELECTED_FC_HZ not in seen
    # Both trim pairs are graded, so the level match runs twice — a single call
    # would mean the guard stopped comparing them (PR-L4's own behaviour).
    assert len(replay.fc_seen["realized_branch_level_match"]) == 2

    # R17: the candidate's sections are USED, not just accepted. Without this,
    # a fit that dropped ``candidate_sections`` and re-read the session's own
    # would pass everything above — the corner it reports is the session's in
    # BOTH worlds, so only the SHAPE separates them. The two shapes are
    # unmistakable: a 1648.7 Hz LR4 radiates (0.0, 1321.3) / (2057.2, inf),
    # a 2000.0 Hz one (0.0, 1602.9) / (2495.5, inf). Both sides are computed
    # through production's own ``radiating_band_hz`` so this pins which
    # SECTIONS reached the fit, not how a band is derived from them.
    for role in ROLES:
        want = radiating_band_hz(replay.candidate_sections[role])
        never = radiating_band_hz(replay.configured_sections[role])
        assert want != never, "the two corners must give different shapes"
        assert replay.fit_radiating_bands[role] == pytest.approx(want), (
            f"#2291 R17: the {role} fit was bounded to "
            f"{replay.fit_radiating_bands[role]}, not the candidate's {want}"
        )

    fit_band = _one_event_line(caplog, "correction.crossover_v2_linearization_fit_band")
    # One line carrying both halves of the contradiction: the corner it names
    # is the session's, the shapes beside it are the candidate's.
    assert f"fc_hz={CONFIGURED_FC_HZ}" in fit_band, f"#2291: {fit_band}"
    assert str(SELECTED_FC_HZ) not in fit_band
    for role in ROLES:
        rendered = str(_rounded_band_hz(radiating_band_hz(
            replay.candidate_sections[role],
        )))
        stale = str(_rounded_band_hz(radiating_band_hz(
            replay.configured_sections[role],
        )))
        assert rendered in fit_band, f"#2291 R17: {role} {rendered} not in {fit_band}"
        assert stale not in fit_band


def _one_event_line(caplog, name: str) -> str:
    """The single rendered log line for one ``log_event`` name."""
    hits = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith(f"event={name} ")
    ]
    assert len(hits) == 1, f"expected exactly one {name} line, got {len(hits)}"
    return hits[0]


# --------------------------------------------------------------------------- #
# defect 2 — "trim_rejected" still commits the rejected trim
# --------------------------------------------------------------------------- #


def test_trim_rejected_still_commits_the_ripple_scan_trim(monkeypatch, caplog):
    """CURRENT behaviour, and it is the bug. #2291 Phase 2 changes it.

    The scan drifts 6.300 dB from the anchor, past the 6.0 dB sanity margin, so
    the outcome stamped on the candidate is ``trim_rejected`` — and the scan's
    trim is committed anyway, because it levels better than the anchor. The
    household-visible artifact says one thing and the emitted gain is the other.

    Everything asserted here is production's own arithmetic on banked inputs:
    the anchor from the incident's give-back and level-frame offsets, the drift
    against the shipped margin constant, and the commit choice.
    """
    caplog.set_level("WARNING", logger="jasper.active_speaker.crossover_v2_flow")
    replay = _run_replay(monkeypatch)

    assert replay.candidate.linearization_outcome == "trim_rejected", (
        "#2291: the incident's outcome string"
    )
    assert dict(replay.candidate.role_attenuations_db) == pytest.approx(COMMITTED_DB), (
        "#2291: the incident's committed pair"
    )

    # Production's own anchor, read off the trim pair it graded — the exact
    # number, not the 3-decimal one the journal rounds to.
    anchor = replay._pair(scan=False)
    scan = replay._pair(scan=True)
    assert anchor["trim_t_db"] == pytest.approx(ANCHORED_DB["tweeter"], abs=1e-12)
    assert anchor["trim_w_db"] == pytest.approx(ANCHORED_DB["woofer"], abs=1e-12)
    drift_db = abs(scan["trim_t_db"] - anchor["trim_t_db"])
    assert drift_db == pytest.approx(
        EXPECTED_OUTCOME["anchor_replay"]["anchor_drift_db"], abs=1e-12
    )
    assert drift_db > LINEARIZATION_TRIM_SANITY_MARGIN_DB

    # The defect: the guard fired — so the outcome reads "trim_rejected" and the
    # WARNING is in the journal — and the rejected pair is nonetheless the pair
    # the graph runs.
    assert (
        "event=correction.crossover_v2_linearization_trim_rejected" in caplog.text
    ), "#2291: the guard's own WARNING"
    assert replay.candidate.role_attenuations_db["tweeter"] == pytest.approx(
        scan["trim_t_db"], abs=1e-12
    ), "#2291: today a rejected trim is still the trim that ships"
    assert replay.candidate.role_attenuations_db["tweeter"] != pytest.approx(
        anchor["trim_t_db"]
    )

    # The replay's own premise, asserted rather than assumed: the incident's
    # record proves the scan won the level comparison (the pair it committed is
    # the scan's), so a synthetic branch pair that failed to reproduce that
    # condition would be pinning a different decision under this test's name.
    assert abs(scan["difference_db"]) < abs(anchor["difference_db"])
