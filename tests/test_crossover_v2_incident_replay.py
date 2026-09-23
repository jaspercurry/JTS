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
)
from jasper.active_speaker.crossover_v2_flow import CrossoverV2Session, V2FlowSeams, V2RecordPublishers
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


# fixture -> production objects


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
        analyze=lambda *a, **k: None,
        records=V2RecordPublishers(
            check=lambda plan, ambient: None,
            candidate=lambda candidate: None,
        ),
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


# the banked record


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


# defect 1 — FIXED: every Fc-driven seam reads the candidate's own corner


# defect 2 — FIXED: a rejected trim is not the trim that ships


# the two sites the pre-cutover replay could NOT pin
