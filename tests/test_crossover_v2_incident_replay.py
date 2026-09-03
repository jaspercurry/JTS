# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free replay of the 2026-08-10 jts3 crossover incident (#2291).

**These tests pinned behaviour that was WRONG until #2291 Phase 2b, and now
pin the fix through the production path.** Phase 0 wrote them as
characterization tests — describing what the prescription path did, defects
included, so the phase that changed it had to state which pinned number it was
moving and why. Phase 2b is that change: the assertions below are the same
seams, flipped, and a green run is now the acceptance evidence that the
incident **cannot** reproduce.

They drive real production code — ``_build_candidate`` at the exact keyword
pair a caller hands it — so this is the *wired* path, not the
planner in isolation (which
``tests/test_crossover_v2_intervention_dual_run.py`` covers).

The incident, in one paragraph. A stage-1 Fc comparison ran on a session
configured at 2000 Hz, evaluated six corners, and recommended 1648.7 Hz. The
candidate it published for that corner carried a −13.013 dB tweeter trim, a
0.0 dB woofer trim, and the outcome string ``trim_rejected`` — and was applied.
Post-apply the speaker measured a failing absolute claim (5.456 dB over a
2.0 dB tolerance) and 7.727 dB of cloud flatness error over 250-2000 Hz against
a 1.5 dB tolerance. Two defects in the prescription path are visible in that
record; each has a test below, and each test says what it pinned before and
what it pins now:

1. The fitter read ``self._fc_hz`` — the SESSION's configured corner — at every
   Fc-driven site, while the candidate it was fitting arrived with
   ``candidate_sections`` at its OWN corner. Every non-configured candidate was
   therefore levelled and ripple-scanned at the wrong crossover. Since Phase 2b
   the planner reads one corner, from a
   :class:`~jasper.active_speaker.crossover_v2.contracts.CandidateAcousticContext`
   the conductor builds from those same sections, and there is no session
   corner in its scope to read instead.
2. ``trim_rejected`` named the outcome when the ripple scan drifted past
   ``LINEARIZATION_TRIM_SANITY_MARGIN_DB`` from the anchor — and the scan's
   trim was still COMMITTED whenever it levelled better. The string said
   rejected; the number that shipped was the rejected one. Since Phase 2b a
   beyond-margin scan IS rejected: the level-preserving anchor ships, the
   outcome string stopped lying because the behaviour changed to match it, and
   the strategy names which pair won.

The evidence is banked raw and SHA-verified under
``captures/jts3-incident-20260810-issue2291/`` (93 MB, gitignored). The small
JSON set these tests read is derived from it by
``scripts/derive-crossover-incident-fixture.py``, which has a ``--check`` mode.

**What replays exactly, and what does not.** Every scalar the decision path
consumes — the raw trim, both fits' core-band give-back, the correction
filters, the ripple scan's own result, the session and candidate corners — is
the incident's, so the drift verdict, the outcome string, the commit choice
and the committed pair are all computed by production from banked numbers and
match the incident exactly.

**One term stopped being purely banked on 2026-08-19, and it is the anchor's.**
The give-back the anchor spends is now MEASURED over ``branch_level_bands_hz``
rather than read off the fit, so it is computed from the synthetic branches
below rather than from a banked scalar. Two things follow. The anchored trim
here is production's arithmetic over a fixture, not a bit-for-bit reproduction
of a number the incident emitted — and it could not be either way, because the
incident predates the band it is now measured in. What the anchor is still
anchored to IS banked: the raw measured trim (−10.8846), which is why the
DIRECTION the fix moves it — 1.252 dB closer to that trim — is a claim about
this incident and not about the fixture.

The per-driver measured RESPONSES do not replay, and the reason is size, not
absence. They were never retained as arrays; re-deriving them offline from
``measure_program.wav`` plus the UMIK-2 calibration is possible, but both
inputs are gitignored capture data and the analysis grid is too large to
commit — the same session's VERIFY frame graded 37,080 bins across 1.7 kHz, so
a full-band complex response runs to ~5e5 bins per driver. The branches below
are
therefore synthetic, and the two seams whose true output needs them —
``fit_driver_linearization`` and ``solve_ripple_optimal_trim`` — return the
incident's own recorded results instead.

One consequence is worth stating rather than leaving to be inferred: the
``difference_db`` values the commit decision turns on are computed by real code
over those synthetic zero-phase branches, so they are not the incident's own
level errors. What the incident's record proves is their ORDERING **at the
session's corner** — it committed the scan's pair there, so the scan levelled
better at 2000 Hz.

At the CANDIDATE's corner the ordering reverses: the anchor levels better, and
the replay asserts that rather than assuming either way. So the two defects
were not independent — fixing the corner alone would already have shipped the
anchor on this session — which is why the rejection policy is what the test
credits, and why the drift verdict rather than the grading is what the
assertions turn on. #2313's dual run reached the same conclusion against live
legacy; this is it restated on the wired path.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import (
    CrossoverSection,
    crossover_response_db,
    radiating_band_hz,
    sections_by_role,
)
from jasper.active_speaker.crossover_v2 import intervention as iv
from jasper.active_speaker.crossover_v2.contracts import TrimStrategy
from jasper.active_speaker.crossover_v2.intervention import (
    rounded_band_hz as _rounded_band_hz,
)
from jasper.active_speaker.crossover_v2_flow import (
    LINEARIZATION_TRIM_SANITY_MARGIN_DB,
    CrossoverV2Session,
    V2FlowSeams,
)
from jasper.active_speaker.linearization_fit import LinearizationFilter, LinearizationFit
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand
from jasper.audio_measurement.program_analysis import (
    REALIZED_LEVEL_MATCH_TOLERANCE_DB,
    ALIGNMENT_OK,
    AlignmentEstimate,
    CrossoverCandidate,
    DriftEstimate,
    DriverResponse,
    ProgramAnalysis,
    SegmentLocation,
    predicted_branch_sum,
)
from tests.crossover_v2_fixtures import _candidate_sections

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
# The anchor these numbers pin is the one design SSOT:
# docs/active-speaker-tuning-layers-design.md, "Anchored give-back (the trim)"
# — the committed RAW trim plus that branch's measured give-back, shared-shift
# normalized non-positive. No third term.
#
# Re-derived 2026-08-17 (#2609). The prior banked pair carried PR-L5's
# ``level_frame_offset_db`` (woofer +1.5644, tweeter 0.0), which was the
# two-voter arbitration's limb rather than an independent measured fact: it
# substituted the shared-frame solve for the raw trim. Deleting it moves the
# tweeter -6.713 -> -5.149 and the shift 2.900 -> 1.335. The tweeter's raw
# measured trim here is -10.8846 — and #2609's conviction is that THAT was the
# right number all along (the reigning tune sat at -10.214).
#
# Re-derived again 2026-08-19 for the give-back BAND fix, which is downstream
# of #2609 rather than a revision of it. The give-back the anchor spends is now
# measured over ``branch_level_bands_hz`` — the bands that solved the raw trim
# and that grade the committed pair — instead of over each driver's own CORE
# band, and on this incident that moves the tweeter -5.149 -> -6.401 and the
# shift 1.335 -> 3.916. **The anchor moved 1.252 dB CLOSER to the raw measured
# -10.8846 this session's own solve asked for**: the core-band term had been
# handing a horn tweeter back level from a band the verdict never reads, which
# is precisely the hot-tweeter error the fix removes, showing up here on an
# incident that was captured long before it.
ANCHORED_DB = EXPECTED_OUTCOME["anchor_replay"]["anchored_trim_db"]

#: The same replay at the ONE-SIDED Fc (the tweeter's sweep floor), where the
#: ripple polish is skipped. It gets its own number because the give-back is
#: now Fc-DEPENDENT: it is measured over ``branch_level_bands_hz``, and those
#: bands are mirrored halves about Fc, so moving Fc moves the band the
#: give-back is read in. Under the old core-band rule the give-back ignored Fc
#: entirely and both paths landed on one value — that they now differ by
#: 0.043 dB is the fix working, not two fixtures disagreeing.
ANCHORED_AT_ONE_SIDED_FC_DB = {"woofer": 0.0, "tweeter": -6.443080668475005}


# --------------------------------------------------------------------------- #
# fixture -> production objects
# --------------------------------------------------------------------------- #


def _incident_fit(role: str) -> LinearizationFit:
    """Rebuild one role's ``LinearizationFit`` from the banked candidate.

    ``candidate.json``'s per-role linearization block IS the serialized fit, so
    this is deserialization, not reconstruction — the give-back the anchor is
    built from is the fit engine's own number from the incident, not a number
    this test chose.
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
        lift_requested_db=banked["lift_requested_db"],
        lift_from_boost_db=banked["lift_from_boost_db"],
        lift_from_reduced_cuts_db=banked["lift_from_reduced_cuts_db"],
        lift_suppressed_reason=banked["lift_suppressed_reason"],
    )


def _session_preset() -> ActiveSpeakerPreset:
    """The preset the SESSION ran, rebuilt from the candidate's own copy.

    The build publishes each candidate with the session preset
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
    scan's trim WON the realized-level comparison **at the session's corner**
    (the committed pair is the scan's, not the anchor's), and a tweeter that
    hot is what made the scan's −13.013 dB look like the level-correct answer
    there. At the candidate's own corner the same branches order the two pairs
    the other way; the replay asserts whichever ordering it gets rather than
    assuming one — see
    ``test_a_rejected_trim_is_not_the_trim_that_ships``.

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


def _conductor() -> CrossoverV2Session:
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
    return CrossoverV2Session(
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
        # What the build returned beside the candidate — the planner's own
        # output, which since #2291 Phase 2b is a value rather than a scatter
        # of conductor fields.
        self.linearization: Any = None
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


def _run_replay(
    monkeypatch: pytest.MonkeyPatch, *, candidate_fc_hz: float = SELECTED_FC_HZ,
) -> _Replay:
    """Drive one candidate build at ``candidate_fc_hz`` on a 2000 Hz session.

    The spies live on
    :mod:`jasper.active_speaker.crossover_v2.intervention` since #2291
    Phase 2b — that module is where the Fc-driven seams are now called from,
    and patching the flow's namespace instead would silently spy on nothing.
    """
    replay = _Replay()
    conductor = _conductor()

    real_overlap = iv.overlap_band_hz
    real_match = iv.realized_branch_level_match

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

    monkeypatch.setattr(iv, "overlap_band_hz", spy_overlap)
    monkeypatch.setattr(iv, "realized_branch_level_match", spy_match)
    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", fake_ripple)
    monkeypatch.setattr(iv, "fit_driver_linearization", fake_fit)

    replay.candidate_sections = _candidate_sections(conductor, candidate_fc_hz)
    # What the fit would have been bounded to had it read the SESSION's own
    # preset — the same derivation ``_plan_linearization`` uses on the
    # configured path, so an R17 regression is a difference this replay sees.
    configured = sections_by_role(conductor._preset.crossover_regions)
    replay.configured_sections = {role: configured.get(role, ()) for role in ROLES}
    candidate_preset = replace(conductor._preset, crossover_regions=tuple(
        replace(region, fc_hz=candidate_fc_hz)
        for region in conductor._preset.crossover_regions
    ))
    # ``_build_candidate``, with the exact keyword pair a candidate build
    # hands it for a non-configured corner — the seam where the prescription is
    # computed. Its caller ``_build_measure_candidate`` adds one further gate,
    # which grades the LINEARIZED predicted sum against the raw one; that gate
    # passed on the incident and is orthogonal to both defects, but it cannot
    # pass on synthetic branches without shaping them until it does, and a
    # fixture tuned to satisfy a gate is not evidence about anything.
    replay.candidate, replay.linearization = conductor._build_candidate(
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
# defect 1 — FIXED: every Fc-driven seam reads the candidate's own corner
# --------------------------------------------------------------------------- #


def test_every_fc_driven_seam_reads_the_candidates_corner_not_the_sessions(
    monkeypatch, caplog,
):
    """The #2291 acceptance criterion, through the PRODUCTION path.

    A configured 2000 Hz session evaluating a selected 1648.7 Hz candidate
    cannot read 2000 Hz anywhere inside candidate planning. The Fc-driven
    seams — the overlap band, the ripple scan, and the realized-level match on
    BOTH candidate trim pairs — are handed the corner of the sections the
    candidate is realized with, and the journal line that tells an operator
    which corner the fit ran at names the same one.

    **What this pinned before Phase 2b:** the exact opposite. The fitter read
    ``self._fc_hz`` at every one of these sites while the same call arrived
    with ``candidate_sections`` at the candidate's corner — 2000 Hz of
    levelling and scanning applied to a 1648.7 Hz candidate. The seams are
    unchanged; only the value they see is.

    **Why it is now structural rather than merely correct:** the planner takes
    one ``CandidateAcousticContext``, which owns the corner *and* the sections
    together and refuses at construction if they disagree. There is no session
    corner in its scope to read by mistake.

    Separately, this pins that the candidate's sections are USED and not merely
    accepted (R17) — see the radiating-band assertions at the end.
    """
    caplog.set_level("INFO", logger="jasper.active_speaker.crossover_v2_flow")
    replay = _run_replay(monkeypatch)

    for section in replay.candidate_sections.values():
        assert [s.fc_hz for s in section] == [SELECTED_FC_HZ] * len(section)
    assert replay.candidate.source_preset.crossover_regions[0].fc_hz == SELECTED_FC_HZ

    for seam, seen in replay.fc_seen.items():
        assert seen, f"{seam} was never reached — the replay did not exercise the fit"
        assert set(seen) == {SELECTED_FC_HZ}, (
            f"#2291: {seam} saw {sorted(set(seen))}; it must read the candidate's "
            f"corner {SELECTED_FC_HZ}, never the session's {CONFIGURED_FC_HZ}"
        )
        assert CONFIGURED_FC_HZ not in seen
    # Both trim pairs are graded, so the level match runs twice — a single call
    # would mean the planner stopped comparing them (PR-L4's own behaviour).
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
    # One line, internally consistent: the corner it names and the shapes
    # beside it are the same candidate's. It used to carry both halves of the
    # contradiction — the session's corner against the candidate's shapes.
    assert f"fc_hz={SELECTED_FC_HZ}" in fit_band, f"#2291: {fit_band}"
    assert str(CONFIGURED_FC_HZ) not in fit_band
    for role in ROLES:
        # ``list(...)`` because the planner's payload crosses ``JournalRecord``,
        # which detaches through JSON containers — so a band that legacy
        # rendered as a Python tuple renders as a JSON array. Same numbers,
        # same order, one container; ``JASPER_LOG_JSON=1`` output is unchanged
        # either way. ``_rounded_band_hz`` is still the one owner of the
        # numbers, which is what this line is about.
        rendered = str(list(_rounded_band_hz(radiating_band_hz(
            replay.candidate_sections[role],
        ))))
        stale = str(list(_rounded_band_hz(radiating_band_hz(
            replay.configured_sections[role],
        ))))
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
# defect 2 — FIXED: a rejected trim is not the trim that ships
# --------------------------------------------------------------------------- #


def test_a_rejected_trim_is_not_the_trim_that_ships(monkeypatch, caplog):
    """The other #2291 acceptance criterion, through the PRODUCTION path.

    The scan drifts 6.612 dB from the anchor, past the 6.0 dB sanity margin, so
    the outcome stamped on the candidate is ``trim_rejected`` — and the pair
    that ships is now the level-preserving ANCHOR, −6.401 dB, under the
    strategy ``ANCHORED_COMMITTED_AFTER_SANITY_DRIFT``. The household-visible
    artifact and the emitted gain say the same thing.

    **What this pinned before Phase 2b:** the same outcome string against the
    scan's own −13.013 dB, because the grading committed whichever pair levelled
    better *regardless of whether the scan had been rejected*. The extra
    6.612 dB of tweeter cut is the largest single term in the dark upper half
    the household then measured.

    The outcome string did not change and did not need to: ``"trim_rejected"``
    was already the right word for what the guard found, and it stopped lying
    because the behaviour changed to match it rather than because it was
    renamed.

    Everything asserted here is production's own arithmetic: the anchor from
    the incident's banked raw trim plus the give-back re-measured in that
    trim's own band (see the module docstring on what that changed about
    "banked"), the drift against the shipped margin constant, and the commit
    choice.
    """
    caplog.set_level("WARNING", logger="jasper.active_speaker.crossover_v2_flow")
    replay = _run_replay(monkeypatch)

    assert replay.candidate.linearization_outcome == "trim_rejected", (
        "#2291: the incident's outcome string, now true"
    )
    assert dict(replay.candidate.role_attenuations_db) == pytest.approx(ANCHORED_DB), (
        "#2291: the honest anchored fallback, not the incident's committed pair"
    )
    assert dict(replay.candidate.role_attenuations_db) != pytest.approx(COMMITTED_DB)

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

    # The fix: the guard fired — so the outcome reads "trim_rejected" and the
    # WARNING is in the journal — and the pair the graph runs is the anchor.
    assert (
        "event=correction.crossover_v2_linearization_trim_rejected" in caplog.text
    ), "#2291: the guard's own WARNING"
    assert replay.candidate.role_attenuations_db["tweeter"] == pytest.approx(
        anchor["trim_t_db"], abs=1e-12
    ), "#2291: a rejected trim must not be the trim that ships"
    assert replay.candidate.role_attenuations_db["tweeter"] != pytest.approx(
        scan["trim_t_db"]
    )
    # The strategy names which pair won, so an artifact reader never has to
    # infer it from an outcome string that only encodes the drift verdict.
    assert (
        replay.linearization.realized_level_match is not None
    ), "the build must have produced a realized-level verdict"

    # **The two defects were not independent, and this is where that shows.**
    # The pre-cutover version of this test asserted the opposite comparison as
    # its premise — the incident's record proves the SESSION-corner grading
    # committed the scan's pair, so at 2000 Hz the scan levelled better. At the
    # CANDIDATE's own corner it does not: the anchor wins the comparison
    # outright, which is #2313's dual-run finding restated on the wired path
    # (``test_at_the_candidates_corner_the_level_grading_already_prefers_the_
    # anchor``). Fixing the corner alone would therefore already have shipped
    # the anchor here.
    #
    # That is not an argument for dropping the fallback policy, and the number
    # above says why: the drift is 6.612 dB against a 6.0 dB margin, so the
    # anchor is committed under ANCHORED_COMMITTED_AFTER_SANITY_DRIFT — the
    # REJECTION, not the grading — and the policy is what covers the
    # session-corner-wild regime where the grading points the other way.
    #
    # The anchor now grades CLEARLY AHEAD, and the margin is the fix's own
    # doing. This assertion has been written three ways as the anchor moved,
    # which is worth stating rather than quietly re-tuning:
    #
    #   * banked arbitration      anchor clearly ahead
    #   * #2609 (offset deleted)  a TIE — 3.940 against the scan's 3.924, and
    #                             a strict inequality on 0.016 dB would have
    #                             pinned nothing but rounding
    #   * band-matched give-back  anchor 2.688 against the scan's 3.924
    #
    # The give-back moved into the trim's own band, so the anchored pair is the
    # one that actually level-matches and it grades 1.236 dB better. That is a
    # real margin rather than rounding, so a strict inequality is now the
    # honest pin — and it is the fix's mechanism showing up on an independent
    # incident, not a fixture drifting.
    #
    # (What used to stand here — "both pairs still miss the 3.0 dB tolerance" —
    # was true of the old anchor and is contradicted 25 lines below by this same
    # block's own assertions: the band-matched anchor CLEARS it at 2.688 while
    # the scan pair still misses at 3.924. Deleted rather than softened.)
    assert abs(anchor["difference_db"]) < abs(scan["difference_db"]), (
        "the band-matched anchor should grade BETTER than the scan pair at "
        "the candidate's corner; if the scan wins, the give-back is no longer "
        "being measured in the band the level instrument grades"
    )
    assert abs(abs(anchor["difference_db"]) - abs(scan["difference_db"])) == (
        pytest.approx(1.236, abs=1e-3)
    ), "the anchor's margin over the scan is the give-back band fix's own size"
    # **The anchor now lands INSIDE the realized-level tolerance, and that is
    # the most consequential thing this replay says about the fix.** The
    # incident's anchored pair used to miss the 3.0 dB bar (3.940 dB); measured
    # in the trim's own band it lands at 2.688 dB — it would have cleared the
    # level gate this incident failed. The scan pair still misses at 3.924 dB.
    #
    # The session still REFUSES, and that is asserted elsewhere in this file
    # rather than inferred here: the trim is rejected on SCAN DRIFT
    # (6.612 dB against a 6.0 dB margin, ``strategy=
    # anchored_committed_after_sanity_drift``), which is a different mechanism
    # from the level gate and is untouched by this change. What moved is that
    # the level instrument no longer independently condemns the pair — so the
    # refusal now rests on the drift policy alone, where before it had two
    # reasons.
    assert abs(anchor["difference_db"]) < REALIZED_LEVEL_MATCH_TOLERANCE_DB, (
        "the band-matched anchor should clear the realized-level tolerance on "
        "this incident; if it misses, the give-back is not being measured in "
        "the band the level instrument grades"
    )
    assert abs(scan["difference_db"]) > REALIZED_LEVEL_MATCH_TOLERANCE_DB, (
        "the scan pair still misses it — the anchor's advantage is not that "
        "the bar moved"
    )


def test_the_rejection_journal_names_the_committed_pair_and_its_strategy(
    monkeypatch, caplog,
):
    """The rejection's own WARNING says which pair won, and it is the anchor.

    Split from the assertion above because it grades a different surface: the
    operator-facing journal line rather than the emitted candidate. Before
    Phase 2b this line carried ``committed=resolved`` beside the word
    "rejected" — the contradiction in one string — and had no ``strategy``
    field at all.
    """
    caplog.set_level("WARNING", logger="jasper.active_speaker.crossover_v2_flow")
    replay = _run_replay(monkeypatch)

    line = _one_event_line(
        caplog, "correction.crossover_v2_linearization_trim_rejected"
    )
    assert "committed=anchored" in line, line
    assert "committed=resolved" not in line
    assert (
        f"strategy={TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT.value}" in line
    ), line
    fallback = round(float(ANCHORED_DB["tweeter"]), 3)
    rejected = round(float(COMMITTED_DB["tweeter"]), 3)
    assert f"fallback_trim_db=\"{{'woofer': 0.0, 'tweeter': {fallback}}}\"" in line, line
    # The scan's pair is still disclosed — rejected, not hidden — so live guard
    # telemetry can still distinguish a legitimate optimum from garbage.
    assert f"resolved_trim_db=\"{{'woofer': 0.0, 'tweeter': {rejected}}}\"" in line, line
    assert replay.candidate.role_attenuations_db["tweeter"] == pytest.approx(
        ANCHORED_DB["tweeter"], abs=1e-12
    )


# --------------------------------------------------------------------------- #
# the two sites the pre-cutover replay could NOT pin
# --------------------------------------------------------------------------- #


def test_the_straddle_and_its_skip_journal_read_the_candidates_corner(
    monkeypatch, caplog,
):
    """The two Fc reads the characterization pass had to leave uncovered.

    Before Phase 2b the fitter read ``self._fc_hz`` at six sites. The
    characterization test pinned four; the straddle test that decides whether
    the ripple scan runs, and the ``fc_hz`` field of the
    ``ripple_trim_skipped`` event in that straddle's own else-branch, could not
    be pinned by the incident at all: on this session's overlap band both
    corners straddle identically, so the branch taken is the same either way
    and the else-branch never runs. Mutation confirmed it — the docstring of
    the pre-cutover test said to treat them as covered by inspection only.

    They are pinnable now, and this is the pin. The overlap band is derived
    FROM the same corner the straddle tests, so a candidate sitting exactly at
    the tweeter's 1600 Hz sweep floor — which clamps the band's lower edge —
    gets a band that STARTS at its own corner and therefore does not straddle
    it, while the session's 2000 Hz still sits inside 1600-4000 Hz. A planner
    reading the session corner would run the scan; reading the candidate's, it
    skips and says so.

    1600 Hz rather than something lower: below the tweeter's sweep floor the
    realized-level estimator refuses outright (it has no excited tweeter band
    reaching the corner), so the plan degrades to trims-only before the
    straddle's consequences can be observed. The sweep floor is the one corner
    where the scan is skipped and the rest of the plan still runs — and it is
    this session's own declared floor, so it is a corner the selector could
    genuinely have proposed.
    """
    caplog.set_level("INFO", logger="jasper.active_speaker.crossover_v2_flow")
    tweeter_sweep_lo_hz = float(SESSION_CONTEXT["sweep_band_hz"]["tweeter"][0])
    one_sided_fc_hz = tweeter_sweep_lo_hz
    assert one_sided_fc_hz < CONFIGURED_FC_HZ, (
        "the premise: the session's corner sits inside the swept overlap and "
        "the candidate's sits on its lower edge"
    )
    assert one_sided_fc_hz == float(
        SESSION_CONTEXT["fc_selection"]["limits"]["declared_floor_hz"]
    ), "and it is a corner this session could actually have proposed"

    replay = _run_replay(monkeypatch, candidate_fc_hz=one_sided_fc_hz)

    # A3 — the straddle itself. Reading the session's 2000 Hz would have run
    # the scan, because 1600 < 2000 < 4000.
    assert replay.fc_seen["solve_ripple_optimal_trim"] == [], (
        "the ripple scan ran on a band that does not straddle the candidate's "
        "corner — the straddle test read some other corner"
    )
    # A6 — the skip event's own ``fc_hz`` field.
    skipped = _one_event_line(
        caplog, "correction.crossover_v2_linearization_ripple_trim_skipped"
    )
    assert f"fc_hz={one_sided_fc_hz}" in skipped, skipped
    assert str(CONFIGURED_FC_HZ) not in skipped
    assert "reason=ripple_band_one_sided" in skipped

    # With no scan there is no drift, so the anchor is committed on its own
    # terms rather than through the sanity fallback. At THIS Fc the give-back
    # is read over a different pair of mirrored halves, so the anchor differs
    # from the configured-Fc replay's by 0.043 dB — see
    # ``ANCHORED_AT_ONE_SIDED_FC_DB``.
    assert replay.candidate.role_attenuations_db["tweeter"] == pytest.approx(
        ANCHORED_AT_ONE_SIDED_FC_DB["tweeter"], abs=1e-12
    )
    assert replay.candidate.linearization_outcome == "fitted"
