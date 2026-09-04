# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: measurement-honesty gates, alignment/phase wiring, and the eager cloud fit."""

from __future__ import annotations

import dataclasses
import logging
import math
import types
import pytest
from dataclasses import replace
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_CLOUD_GEOMETRY_LOCKED,
    REASON_LOCATE_FAILED,
    REASON_REGISTRY,
    locate_failed_diagnosis,
)
from jasper.active_speaker.crossover_v2_flow import (
    CLOUD_CLOSE_AWAITING_CONFIRM,
    CLOUD_GEOMETRY_RETRY_PROMPTS,
    GEOMETRY_RETRY_POSITIONS,
    PILOT_SNR_UNUSABLE_DB,
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS,
    CrossoverV2Session,
    CrossoverV2FlowError,
    _worst_pilot_snr_db,
    alignment_to_candidate_fields,
)
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    ALIGNMENT_OK,
    SegmentLocation,
)
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginRefused
from tests.crossover_v2_fixtures import (
    CAPS,
    CLOUD_MAP,
    CLOUD_MEASURE_INDEXES,
    FC_HZ,
    FakeSeams,
    SESSION,
    SESSION_VOLUME_DB,
    STAGE2_MAP,
    _DIAG_LOGGER,
    _alignment,
    _check_analysis,
    _cloud_conductor,
    _comb_cloud_analysis_factory,
    _conductor,
    _confirm_cloud,
    _count_builds,
    _eligible_measure_analysis,
    _loc,
    _lock,
    _measure_analysis,
    _preset,
    _roles,
    _run_phase,
    _snr_analysis,
    _snr_pilot,
    _verify_analysis,
    _walk,
    _walk_measure_cloud_to_accept,
    _walk_measure_cloud_to_close,
)


# --- §5.10 failure templates ------------------------------------------------------


def test_clipped_measure_is_transient_auto_retry_with_quieter_program():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    gain_before = c.program_for_phase(PHASE_MEASURE).segment("sweep_w").gain_db

    fakes.measure = lambda program: _measure_analysis(program, clipped=True)
    verdict = _run_phase(c, 2, 2)
    assert verdict == {
        "accepted": False,
        "code": "clipped",
        "template": "silent_auto_retry",
        "reason": REASON_REGISTRY["clipped"].banner,
        "banner": REASON_REGISTRY["clipped"].banner,
        "auto_retry": True,
        # See the same key in
        # `test_low_alignment_confidence_rejects_measure_before_building_candidate`
        # — the pilot evidence rides every rejection (#2085), not only the
        # codes whose copy currently branches on it.
        "pilot_heard": None,
        # The honest per-position count rides EVERY verdict (#2086 item 2).
        # This rejection was the slot's PLANNED capture, so nothing is spent
        # yet and all three extras are still on offer.
        "attempts": {
            "used": 0, "allowed": 3, "left": 3,
            "by_speaker": 0, "by_household": 0,
        },
    }
    # The automatic retry is gain-adjusted: 3 dB quieter. This literal is the
    # only tripwire for ``crossover_v2_flow.CLIP_RETRY_BACKOFF_DB``, which
    # nothing in the tree imports — importing it here would make the assertion
    # pass at any value.
    gain_after = c.program_for_phase(PHASE_MEASURE).segment("sweep_w").gain_db
    assert gain_after == pytest.approx(gain_before - 3.0)
    # Retry (same index, next attempt) succeeds.
    fakes.measure = _measure_analysis
    assert _run_phase(c, 2, 3)["accepted"] is True


def test_glitch_reuses_drift_baselines_disagree():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(program, glitch=True)
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "drift_baselines_disagree"
    assert verdict["template"] == "silent_auto_retry"
    assert verdict["auto_retry"] is True


# --- measurement-honesty gate G2: sweep schedule-integrity (xrun detector) ------


def test_sweep_schedule_fires_on_large_residual_even_with_good_confidence():
    """Measurement-honesty gate G2 (2026-07-22 — the xrun detector): a
    uniform whole-capture schedule shift the repeat-pair drift check above
    is structurally blind to. Mirrors the 2026-07-22 ``event=outputd.xrun``
    hardware evidence's -25...-28 ms shift, isolating the RESIDUAL half of
    the gate: good confidence (0.8, clears SWEEP_LOCATE_CONFIDENCE_FLOOR)
    does not save a badly-shifted sweep. Routed identically to the
    pre-existing glitch branch above — same silent auto-retry, same reused
    drift_baselines_disagree code (§5.2's capture-glitch reuse convention);
    the diag ``guard`` field is what tells them apart in telemetry."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        sweep_locations=(
            _loc("sweep_w", confidence=0.8,
                 residual_samples=-25e-3 * program.sample_rate_hz),
            _loc("sweep_t", confidence=0.8),
            _loc("sweep_w_rep", confidence=0.8),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "drift_baselines_disagree"
    assert verdict["template"] == "silent_auto_retry"
    assert verdict["auto_retry"] is True
    # The automatic retry recomposed the MEASURE program (§5.10 t1, mirrors
    # test_clipped_measure_is_transient_auto_retry_with_quieter_program) and
    # left the conductor in a working state — a clean re-capture succeeds.
    fakes.measure = _measure_analysis
    assert _run_phase(c, 2, 3)["accepted"] is True


def test_weakly_located_sweep_reads_too_quiet_not_glitched():
    """D3 (#1838): the CONFIDENCE half of G2 is a LEVEL verdict, not a glitch.

    Mirrors the 2026-07-22 xrun evidence's 0.07-0.12 per-segment confidence
    with a negligible residual, so only the confidence floor is exercised.
    0.12 clears LOCATE_MIN_CONFIDENCE (0.1) but is under
    SWEEP_LOCATE_CONFIDENCE_FLOOR (0.3).

    Until #1838 this returned `drift_baselines_disagree` + a silent auto
    retry — the household was told its capture had glitched, and the flow
    re-ran the same level. A sweep the locator can barely find was not
    spliced; it was too quiet to hear, and re-running it at the same level
    cannot succeed. `locate_failed` says so and does not auto-retry.

    WHICH sentence it says is no longer fixed: since #2085 the copy is chosen
    from this capture's own pilot evidence, because "too quiet to hear" is an
    inference the pilot can refute. This scenario's analysis carries no pilot
    verdict, so it renders the unknown-evidence copy; the two established
    branches are pinned in `test_crossover_v2_honest_capture_copy.py`.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        sweep_locations=(
            _loc("sweep_w", confidence=0.12, residual_samples=1.0),
            _loc("sweep_t", confidence=0.12),
            _loc("sweep_w_rep", confidence=0.12),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "locate_failed"
    # Positive assertion: the household is asked to fix the level and retry,
    # not silently re-run at the same one. (`!= "silent_auto_retry"` would
    # also pass if the template were renamed or dropped.)
    assert verdict["template"] == "fix_and_retry"
    assert not verdict.get("auto_retry")


def test_buried_measure_capture_reads_too_quiet_not_glitched():
    """D3 (#1838), the whole field shape at once: session
    cap_-Us10xORVNlFa_dgi-sP7g's MEASURE played 33 dB below flat, so its
    pilots sank under their SNR floor, its sweeps located at 0.03, the
    mis-located sweeps produced a 1018-sample residual, and the residual
    tripped `glitch_detected` on noise.

    Every one of those is downstream of one cause: nobody could hear the
    capture. With the glitch branch second in the ladder the household was
    told "capture glitched", the flow silently re-armed the same unwinnable
    level, and the session burned 120 s of dead air into a CaptureTimeout.
    The verdict has to name the level.

    The pilots are given real confidence on purpose: they WERE located that
    evening (the SNR guard read 11.22 dB against a 12.38 dB floor, which it
    could only do on a located pair), and they are what let the capture past
    the first `_stimulus_locate_ok` gate.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        pilot_snr_ok=False,
        glitch=True,
        sweep_locations=(
            _loc("pilot_woofer_lo", kind="pilot", confidence=0.5),
            _loc("pilot_woofer_hi", kind="pilot", confidence=0.6),
            _loc("sweep_w", confidence=0.0298, residual_samples=1018.0),
            _loc("sweep_t", confidence=0.0298, residual_samples=1018.0),
            _loc("sweep_w_rep", confidence=0.0298, residual_samples=1018.0),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "pilot_level_collapse"
    assert not verdict.get("auto_retry")

    # And with the pilots healthy, the same buried sweeps still read as a
    # level problem — the weak-locate gate, not the glitch branch.
    fakes.measure = lambda program: _measure_analysis(
        program,
        glitch=True,
        sweep_locations=(
            _loc("pilot_woofer_lo", kind="pilot", confidence=0.5),
            _loc("sweep_w", confidence=0.15, residual_samples=1018.0),
            _loc("sweep_t", confidence=0.15, residual_samples=1018.0),
            _loc("sweep_w_rep", confidence=0.15, residual_samples=1018.0),
        ),
    )
    assert _run_phase(c, 2, 3)["code"] == "locate_failed"


def test_sweep_schedule_clean_capture_passes():
    """The default fixture (well inside both thresholds) is unaffected —
    the happy path already exercises this; pins it explicitly."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True


def test_sweep_schedule_boundary_exact_values_pass():
    """Both thresholds are exclusive bounds (``>``/``<``) — exactly-at the
    ceiling/floor passes."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        sweep_locations=(
            _loc(
                "sweep_w", confidence=SWEEP_LOCATE_CONFIDENCE_FLOOR,
                residual_samples=(
                    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS * 1e-3 * program.sample_rate_hz
                ),
            ),
            _loc("sweep_t", confidence=SWEEP_LOCATE_CONFIDENCE_FLOOR),
            _loc("sweep_w_rep", confidence=SWEEP_LOCATE_CONFIDENCE_FLOOR),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True


def test_sweep_schedule_ignores_pilot_segments():
    """Sweeps-only filter (mirrors ``_estimate_drift``'s own pilot exclusion
    in program_analysis.py): a catastrophically bad PILOT location does not
    fire G2 — only ``KIND_SWEEP`` locations are judged."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        sweep_locations=(
            _loc("pilot_woofer_hi", "pilot", confidence=0.01,
                 residual_samples=-1_000_000.0),
            _loc("sweep_w", confidence=0.9),
            _loc("sweep_t", confidence=0.9),
            _loc("sweep_w_rep", confidence=0.9),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True


def test_stimulus_locate_floor_is_per_role_not_per_capture():
    """D8 (#1838): one clearly-located driver must not clear the gate for a
    driver nobody heard.

    `_stimulus_locate_ok` was `max(confidences) >= LOCATE_MIN_CONFIDENCE`
    across every stimulus segment in the capture — on a two-driver program
    that is effectively no floor at all: a confidently-located woofer let a
    silent tweeter through to be analysed as if it had been measured.

    Per ROLE, not per SEGMENT: a two-level pilot pair's quiet side locates
    more coarsely by design, so the rule is "every role had at least one
    stimulus we could find", not "every segment was easy to find".
    """
    from jasper.active_speaker.crossover_v2_flow import _stimulus_locate_ok

    def _analysis(locations):
        return types.SimpleNamespace(locations=locations)

    def _role_loc(segment_id, role, confidence, kind="sweep"):
        return SegmentLocation(
            segment_id=segment_id, kind=kind, role=role,
            scheduled_start=0, located_start=0, residual_samples=0.0,
            confidence=confidence, peak_dbfs=-12.0, clipped=False,
        )

    # The hole this closes: woofer loud and clear, tweeter inaudible.
    assert not _stimulus_locate_ok(_analysis((
        _role_loc("sweep_w", "woofer", 0.9),
        _role_loc("sweep_t", "tweeter", 0.02),
    )))
    # Both heard: passes.
    assert _stimulus_locate_ok(_analysis((
        _role_loc("sweep_w", "woofer", 0.9),
        _role_loc("sweep_t", "tweeter", 0.4),
    )))
    # A role's weak quiet pilot does NOT sink a role that also has a
    # confidently-located segment.
    assert _stimulus_locate_ok(_analysis((
        _role_loc("pilot_woofer_lo", "woofer", 0.05, kind="pilot"),
        _role_loc("sweep_w", "woofer", 0.9),
        _role_loc("sweep_t", "tweeter", 0.4),
    )))
    # Nothing located at all is still a failure.
    assert not _stimulus_locate_ok(_analysis(()))


def test_locate_failed_and_budget_exhaustion():
    """The planned capture plus THREE extra tries, then the honest end.

    Transformed from a per-code budget (this reason's ``retry_budget`` of 1 gave
    two attempts total) to the owner's pooled per-position bound (#2086). CHECK
    is a single-capture phase: there are no other positions to proceed with, so
    exhaustion ends the session — but the refusal names the spent tries, and
    the code it attributes is the condition actually observed, never a generic
    exhaustion code.
    """
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, locate_confidence=0.01)
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["code"] == "locate_failed"
    assert verdict["template"] == "fix_and_retry"
    # The planned capture spent nothing; three extras are on offer, and the
    # count the phone renders says so.
    assert verdict["attempts"] == {
        "used": 0, "allowed": 3, "left": 3, "by_speaker": 0, "by_household": 0,
    }
    for extra in (1, 2, 3):
        verdict = _run_phase(c, 1, 1 + extra)
        assert verdict["code"] == "locate_failed"
        assert verdict["attempts"]["used"] == extra
        assert verdict["attempts"]["left"] == 3 - extra
        # The household asked for every one of them — nothing was system-forced.
        assert verdict["attempts"]["by_household"] == extra
        assert verdict["attempts"]["by_speaker"] == 0

    with pytest.raises(CaptureBeginRefused) as excinfo:
        c.authorize_begin(1, 5)
    assert excinfo.value.code == "locate_failed"
    # The copy states the count and the outcome. It must NOT invite another try:
    # that is the exact sentence the ruling forbids in front of a refusal.
    message = excinfo.value.user_message
    assert "4 times" in message and "3 extra tries" in message
    assert message.startswith(locate_failed_diagnosis(verdict["pilot_heard"]))
    assert "cannot continue" in message.lower()
    assert "try again" not in message.lower()


def test_check_agc_and_snr_and_channel_map_verdicts():
    # linearity=False with ambient looking clean (snr_floor_ok defaults True)
    # ⇒ the phone's own AGC is the honest cause.
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, linearity=False)
    c = _conductor(fakes)
    assert _run_phase(c, 1, 1)["code"] == "agc_behavioral_fail"

    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, snr_floor_ok=False)
    c = _conductor(fakes)
    assert _run_phase(c, 1, 1)["code"] == "snr_floor"

    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, channel_map=False)
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["code"] == "channel_map_mismatch"
    assert verdict["template"] == "hard_stop"
    # Hard stop: budget 0 ⇒ the very next begin is refused.
    with pytest.raises(CaptureBeginRefused):
        c.authorize_begin(1, 2)


def test_check_low_pilot_snr_routes_to_snr_floor_not_agc():
    """Band-relative ambient-compensated linearity fix (2026-07-20): when the
    quiet pilot's own in-band SNR is too low to trust the ambient-subtracted
    estimate, ``program_analysis`` forces ``linearity_ok`` True (never a false
    linearity FAILURE) and flags ``pilot_snr_ok=False`` instead. The conductor
    must route that on its own — before ever reaching the linearity branch —
    to the honest room/positioning reason, never blaming the phone's AGC."""
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, pilot_snr_ok=False)
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["code"] == "snr_floor"
    assert verdict["template"] == "fix_and_retry"


def test_check_with_no_ambient_evidence_refuses_before_publishing_check_json():
    """Issue #1818's degraded path, pinned where it is ENFORCED.

    A capture whose ambient window survived below
    ``AMBIENT_MIN_USABLE_FRACTION`` yields an EMPTY band report, and
    ``_snr_floor_ok`` reads an empty report as ``False`` (pinned one module
    below by
    ``test_audio_measurement_program_analysis.py::test_check_ambient_below_the_usable_fraction_degrades_to_disclosed_no_evidence``).
    This is the other half of that coupling: the conductor must refuse such a
    CHECK with ``snr_floor`` **and must not publish check.json** — a refused
    CHECK that still published would hand MEASURE a gain plan and an ambient
    report the session never actually measured.

    The publish seam is a RAISING stub rather than a recording one on purpose.
    Asserting an empty ``published_checks`` list would pass for the wrong
    reason if the refusal were ever moved BELOW the publish and the list were
    cleared; a stub that raises fails loudly at the moment of the call, and
    names why in the failure text.
    """
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, snr_floor_ok=False)
    c = _conductor(fakes)

    def _must_not_publish(plan, ambient):
        raise AssertionError(
            "check.json was published for a CHECK the conductor refuses: "
            "the snr_floor gate must sit ABOVE publish_check"
        )

    c._seams = dataclasses.replace(c._seams, publish_check=_must_not_publish)

    verdict = _run_phase(c, 1, 1)
    assert verdict["code"] == "snr_floor"
    assert fakes.published_checks == []


def test_check_linearity_fail_blames_the_room_when_ambient_is_elevated():
    """W6.12: agc_behavioral_fail's copy blames the phone's mic, but hardware
    round 4 proved a distinct honest cause with the identical symptom (the
    captured pilot-pair delta drifting from the programmed delta) — a loud
    ambient burst during the pilot pair, with the phone's AGC verifiably off.
    When the SAME capture's ambient bands ALSO fail the CHECK gain solve's own
    SNR-floor verdict (computed unconditionally, independent of linearity),
    the room — not the phone — is named."""
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(
        program, linearity=False, snr_floor_ok=False,
    )
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["code"] == "noisy_room_linearity"
    assert verdict["template"] == "fix_and_retry"


def test_measure_low_pilot_snr_routes_to_level_collapse_not_agc():
    """Issue #1810 at MEASURE.

    The guard existed on ``PilotObservation`` all along, but MEASURE programs
    carried no ambient window, so ``pilot_snr_ok`` could only ever be True
    there and this branch was unreachable. Now that the composer gives them a
    pre-pilot window, a capture whose pilots never cleared the room floor gets
    a verdict about the room and the level — never about the phone.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(program, pilot_snr_ok=False)
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "pilot_level_collapse"
    assert verdict["template"] == "fix_and_retry"


def test_measure_low_pilot_snr_wins_over_the_linearity_branch():
    """Ordering is the whole fix. ``_pilot_observations`` forces
    ``linearity_ok`` True under the SNR floor, but a caller that checked
    linearity FIRST would still route a hand-built analysis carrying both
    flags to the mic accusation — and, more importantly, the ordering is what
    a future analysis change must not be free to invert."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program, linearity=False, pilot_snr_ok=False,
    )
    assert _run_phase(c, 2, 2)["code"] == "pilot_level_collapse"


def test_verify_low_pilot_snr_routes_to_level_collapse_not_agc():
    """Issue #1810 at VERIFY — the JTS3 session of 2026-07-28.

    A freshly-applied correction dropped the pilot band 14-18 dB, the quiet
    pilot landed ~5 dB over the room floor, the noise compressed the captured
    two-pilot delta from 10 dB to 6 dB, and the household was told "your
    phone's microphone changed its own levels" while the only direct
    recording-chain evidence (``pilot_transfer_step_db``) was null.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    fakes.verify = lambda program: _verify_analysis(program, pilot_snr_ok=False)
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == "pilot_level_collapse"
    # Post-apply, the envelope promotes any failure to the verify_fail screen
    # (W6.7 ruling 3) so the household keeps its Undo — the REASON's own
    # template stays fix_and_retry, which is what applies pre-apply.
    assert REASON_REGISTRY["pilot_level_collapse"].template == "fix_and_retry"


def test_verify_low_pilot_snr_does_not_seed_the_g3_transfer_baseline():
    """A collapsed pilot pair cannot establish the G3 reference either.

    ``_verify_verdict`` refuses on SNR BEFORE the transfer block, so a
    low-SNR first attempt leaves no baseline behind — otherwise the next,
    good attempt would be compared against a level measured out of noise and
    could fail ``verify_level_shift`` on the strength of it. This is also the
    bound that keeps ambient subtraction out of G3's error budget (see
    ``_pilot_transfer_by_role``'s docstring).
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_snr_ok=False, pilot_hi_dbfs=-45.0,
    )
    assert _run_phase(c, 3, 3)["code"] == "pilot_level_collapse"
    assert c._verify_pilot_baseline is None
    # The good re-verify then establishes the baseline itself and passes.
    fakes.verify = lambda program: _verify_analysis(program, pilot_hi_dbfs=-20.0)
    assert _run_phase(c, 3, 4)["accepted"] is True


def test_cloud_position_low_pilot_snr_routes_to_level_collapse_not_agc():
    """The same ordering on a prompted cloud position — the phase that walks
    the mic, and so the one most likely to meet a genuinely quiet spot."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.verify = lambda program: _verify_analysis(program, pilot_snr_ok=False)
    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[0], 3)
    assert verdict["code"] == "pilot_level_collapse"


@pytest.mark.parametrize("snrs,expected", [
    # The row the review caught: one pilot buried (-inf, "never exceeded the
    # ambient"), one clean. Dropping -inf as non-finite logged the CLEAN
    # pilot's 20.0 dB beside pilot_snr_ok=False — a diag row contradicting
    # itself, and the same "verdict beside absent evidence" shape #1810 is
    # about. The buried pilot must win the min().
    (( -math.inf, 20.0), PILOT_SNR_UNUSABLE_DB),
    # +inf is NOT a measurement ("no ambient window to validate against"), so
    # it is excluded rather than floored — the real number is reported.
    ((math.inf, 20.0), 20.0),
    # Every pilot +inf (a legacy program with no window at all): no number to
    # report, and None must not be confused with a measured floor.
    ((math.inf, math.inf), None),
    # Both buried.
    ((-math.inf, -math.inf), PILOT_SNR_UNUSABLE_DB),
    # Ordinary case: the worst real number.
    ((30.0, 11.5), 11.5),
])
def test_worst_pilot_snr_db_handles_both_infinities(snrs, expected):
    """The diag field must never contradict the verdict logged beside it."""
    analysis = _snr_analysis(
        *(_snr_pilot(f"r{i}", snr) for i, snr in enumerate(snrs))
    )
    assert _worst_pilot_snr_db(analysis) == expected


def test_worst_pilot_snr_db_is_none_without_pilots():
    assert _worst_pilot_snr_db(_snr_analysis()) is None


def test_pilot_level_collapse_copy_never_accuses_the_phone():
    """Issue #1810's actual complaint, pinned as copy.

    The household's previous experience of this failure was being told to go
    re-allow a microphone that had done nothing wrong. The new reason names
    the two real causes and two real actions; the definite mic accusation is
    reserved for ``verify_level_shift``, which has the cross-attempt transfer
    step to back it.
    """
    spec = REASON_REGISTRY["pilot_level_collapse"]
    assert spec.retry_budget == 1
    text = spec.message.lower()
    assert "phone's microphone" not in text
    assert "re-allow" not in text
    assert "too loud" in text and "too quiet" in text
    # The one code still allowed to state the mic as the cause is the one
    # holding the evidence for it.
    assert "microphone" in REASON_REGISTRY["verify_level_shift"].message.lower()


def test_agc_behavioral_fail_copy_states_the_observation_not_the_cause():
    """Issue #1810 amendment. ``agc_behavioral_fail`` fires on a captured
    two-pilot delta that did not match the programmed one — which the phone's
    input chain OR the speaker's own output compression can produce. The copy
    may describe that observation and prescribe the one useful action; it may
    not assert the phone as the cause, because this code never observes it."""
    message = REASON_REGISTRY["agc_behavioral_fail"].message
    assert "your phone's microphone changed" not in message.lower()
    assert "test tones" in message.lower()


def test_delay_exceeds_search_window_verdict():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        alignment=_alignment(status=ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "delay_exceeds_search_window"
    assert verdict["template"] == "fix_and_retry"


def test_verify_out_of_tolerance_and_inconclusive():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()

    # Out of tolerance: |measured − predicted| > 1.5 dB.
    fakes.verify = lambda program: _verify_analysis(program, max_db=2.4)
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == "verify_out_of_tolerance"
    assert verdict["template"] == "verify_fail"
    assert c.verify_outcome == "fail"

    # Gate-comparability: VERIFY's own gate shorter than MEASURE's ⇒
    # "inconclusive — re-verify", not fail (§5.2).
    fakes.verify = lambda program: _verify_analysis(program, max_db=0.5, gate_ms=5.0)
    verdict = _run_phase(c, 3, 4)
    assert verdict["code"] == "verify_inconclusive"
    assert c.verify_outcome == "inconclusive"

    # A comparable-gate clean re-verify passes (budget 2 admits it).
    fakes.verify = _verify_analysis
    verdict = _run_phase(c, 3, 5)
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"


# --- alignment sign contract -----------------------------------------------------


def test_alignment_to_candidate_fields_sign_contract():
    def analysis_with(delay_us, status=ALIGNMENT_OK, polarity="normal"):
        class _A:
            alignment = _alignment(delay_us=delay_us, status=status, polarity=polarity)
        return _A()

    # positive ⇒ tweeter earlier ⇒ tweeter delayed.
    delay, role, polarity = alignment_to_candidate_fields(
        analysis_with(150.0), roles=("woofer", "tweeter"),
    )
    assert (delay, role, polarity) == (150.0, "tweeter", "keep")
    # negative ⇒ woofer delayed, magnitude non-negative.
    delay, role, polarity = alignment_to_candidate_fields(
        analysis_with(-90.0), roles=("woofer", "tweeter"),
    )
    assert (delay, role, polarity) == (90.0, "woofer", "keep")
    # inverted polarity maps to the W4 "invert" vocabulary.
    delay, role, polarity = alignment_to_candidate_fields(
        analysis_with(150.0, polarity="inverted"),
        roles=("woofer", "tweeter"),
    )
    assert polarity == "invert"
    # An edge-clamped estimate is not applied: trims-only candidate.
    delay, role, polarity = alignment_to_candidate_fields(
        analysis_with(150.0, status=ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW),
        roles=("woofer", "tweeter"),
    )
    assert (delay, role, polarity) == (None, None, None)


# --- phase persistence + session binding (§5.6) -----------------------------------


def test_resume_within_session_skips_accepted_phases():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    snap = c.snapshot()
    assert snap.accepted_phases == (PHASE_CHECK,)

    resumed = CrossoverV2Session.hydrate(
        snap,
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
    )
    assert resumed.current_phase == PHASE_MEASURE
    # The MEASURE program was recomposed from the persisted gain plan.
    program = resumed.program_for_phase(PHASE_MEASURE)
    assert program.segment("sweep_w").gain_db == pytest.approx(-11.0)


def test_new_session_invalidates_check_and_measure_evidence():
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    snap = c.snapshot()
    assert PHASE_MEASURE in snap.accepted_phases

    fresh = CrossoverV2Session.hydrate(
        snap,
        session_id="cap_other_session",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
    )
    assert fresh.accepted_phases == frozenset()
    assert fresh.current_phase == PHASE_CHECK


# --- position-group choreography (flat-linearization PR-3b) ------------------
#
# State-walk tests over the group lifecycle, driven through the fake seams. The
# cloud positions play the VERIFY-shaped summed program, so FakeSeams' analyze
# dispatch (keyed on the PROGRAM's phase) returns `_verify_analysis` for them
# with no new factory — the same reason `program_analysis` needed no new
# dispatch branch.


def test_cloud_measure_group_closes_only_after_its_last_position():
    """One PHASE spans many indexes: accepting position 3 of 8 must not read as
    "the pre-apply cloud is done" — the phase closes on its LAST index."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    assert c.current_phase == PHASE_CLOUD_MEASURE

    for index in CLOUD_MEASURE_INDEXES[:-1]:
        verdict = _run_phase(c, index, attempt)
        attempt += 1
        assert verdict["accepted"] is True
        assert verdict["position_id"]
        assert PHASE_CLOUD_MEASURE not in c.accepted_phases
        assert c.current_phase == PHASE_CLOUD_MEASURE
        assert "group_complete" not in verdict

    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)
    assert verdict["accepted"] is True
    assert verdict["group_complete"] == PHASE_CLOUD_MEASURE
    assert verdict["geometry"]["locked"] is False
    assert PHASE_CLOUD_MEASURE in c.accepted_phases
    # Every position is retained, in capture order, under a stable id.
    assert c.group_positions(PHASE_CLOUD_MEASURE) == tuple(
        f"{PHASE_CLOUD_MEASURE}_{i:02d}" for i in CLOUD_MEASURE_INDEXES
    )
    # The group closed, so its verdict is readable; the group that has not
    # started reports None (never "geometry was fine").
    assert c.group_geometry(PHASE_CLOUD_MEASURE) is not None
    assert c.group_geometry(PHASE_CLOUD_VERIFY) is None
    # …but the FIT has NOT run yet (flow-simplification §2.6): the geometry
    # close is a per-capture verdict, the fit waits for the household's
    # confirmation past the final position, so that position stays retakeable.
    assert verdict["awaiting_confirm"] is True
    assert c.candidate is None
    assert c.cloud_measure_group_awaiting_confirm() is True
    # The household's explicit confirmation is what builds the candidate — it
    # used to ride the next entry's begin, which a measure-only plan does not
    # have (two-stage work order D1).
    assert _confirm_cloud(c)["candidate_fingerprint"]
    assert c.candidate is not None
    assert c.cloud_measure_group_awaiting_confirm() is False
    # …and the measuring SESSION is over: it has no VERIFY entry to hold for,
    # and nothing was applied. What comes next is the review interlude on
    # jts.local, which the wizard's own phase resolution owns (a measure-only
    # `session_phases` with `applied` false resolves to `review`, never
    # `done` — see tests/test_correction_crossover_v2_endpoints.py).
    assert c.current_phase == PHASE_DONE
    assert PHASE_VERIFY not in c.session_phases


# --- the timing move + the cloud→fit wiring (flat-linearization PR-6b) -------
#
# Owner decision (2026-07-27): the fit, the candidate build, and the auto-apply
# trigger move from MEASURE's accept to the CLOUD_MEASURE group close, so the
# fit consumes the cloud's honesty verdict instead of preceding it by eight
# captures. These walk the REAL conductor for both halves of that: WHEN the
# candidate appears, and WHAT reaches the envelope when it does.


def test_the_candidate_is_built_at_the_cloud_group_close_not_at_measure():
    """The timing move, at the conductor's own surface.

    MEASURE still ACCEPTS — every trust gate it owns is unchanged and still
    fires there — but it no longer produces a candidate, a fingerprint, or the
    ``auto_apply`` flag. All three appear once the pre-apply cloud is walked
    AND confirmed, eight captures later, which is the first moment the fit has
    a cloud verdict to consume.

    Flow-simplification §2.6 moved the trigger one tap further: the final
    position's ACCEPTANCE closes the geometry and stashes the combine, and the
    household's confirmation past it is what fits. So the candidate appears on
    the confirm, not on that last verdict.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)

    measure_verdict = _run_phase(c, 1, 1) and _run_phase(c, 2, 2)
    assert measure_verdict["accepted"] is True
    assert measure_verdict["measurement_phase"] == PHASE_MEASURE
    assert "candidate_fingerprint" not in measure_verdict
    assert "auto_apply" not in measure_verdict
    assert c.candidate is None
    assert fakes.published_candidates == []

    attempt = 3
    for index in CLOUD_MEASURE_INDEXES[:-1]:
        verdict = _run_phase(c, index, attempt)
        attempt += 1
        assert "auto_apply" not in verdict
        assert c.candidate is None, index

    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)
    assert verdict["accepted"] is True
    assert verdict["group_complete"] == PHASE_CLOUD_MEASURE
    # Walked, not yet confirmed: no fit, no publish, nothing applied — so a
    # household that stops here leaves the speaker untouched.
    assert "auto_apply" not in verdict
    assert c.candidate is None
    assert fakes.published_candidates == []

    confirmed = _confirm_cloud(c)
    assert confirmed["candidate_fingerprint"] == c.candidate.fingerprint
    # …and it carries no apply trigger (D1).
    assert "auto_apply" not in confirmed
    assert len(fakes.published_candidates) == 1
    # A second confirm is a no-op — the fit fires exactly once per session.
    assert c.confirm_cloud_measure_group() is None
    assert len(fakes.published_candidates) == 1
    # And the measuring session ends here — nothing applied, nothing held.
    assert c.current_phase == PHASE_DONE


# --- the eager fit (owner UX direction, 2026-07-30) ------------------------------


def test_a_speculative_candidate_does_not_release_the_held_set():
    """**The load-bearing pin of the eager-fit rider.**

    Both seams that resolve the held set carried a comment warning that
    ``cloud_measure_group_awaiting_confirm`` answered "has the household
    confirmed?" with ``self._candidate is None`` — which is also the group
    close's fire-once guard. An eagerly-built candidate would therefore have
    flipped the predicate to False and un-held the runner's set, shutting the
    voluntary-retake window in the same instant it opened, silently, at the one
    moment the design exists to keep it open.

    So: fit early, and the window must not move. The predicate now reads
    ``_group_confirmed``, and an eager build parks somewhere nothing else
    looks.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_accept(c)

    assert c.cloud_measure_group_awaiting_confirm() is True
    assert c.run_speculative_group_close() is True

    # THE PIN: a candidate now exists, fitted and gated, and the household's
    # window is exactly as open as it was a line ago.
    assert c.cloud_measure_group_awaiting_confirm() is True
    # …because none of the three things that make a candidate real happened.
    assert c.candidate is None
    assert fakes.published_candidates == []
    # And the speaker page still says what is TRUE — the household has
    # something to do and it is on their phone. The eager fit is deliberately
    # invisible: "running" is reserved for work the household has asked for,
    # and a retake would otherwise have to walk that state backwards.
    assert c.cloud_close_state == CLOUD_CLOSE_AWAITING_CONFIRM

    # Only the household's own confirmation moves any of it.
    assert _confirm_cloud(c)["candidate_fingerprint"]
    assert c.cloud_measure_group_awaiting_confirm() is False
    assert len(fakes.published_candidates) == 1


def test_the_confirm_commits_the_eager_fit_rather_than_refitting():
    """The payoff: the household's Continue costs a COMMIT, not a fit.

    The whole point of the rider — the fit is the slowest thing in the session
    (a measured 2.7-6 s combine plus the fit itself, worse on a Pi 5) and it
    used to start only once the household had walked back to a browser and
    tapped. Here it has already run, so the tap publishes a finished candidate
    and the review screen is up immediately.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_accept(c)
    builds = _count_builds(c)

    assert c.run_speculative_group_close() is True
    assert len(builds) == 1
    banked = c._speculative_close.candidate
    # Idempotent: the host fires the trigger on every accept that leaves a
    # walked, unconfirmed cloud, and a retake makes that more than once. A
    # second eager fit while one is already banked must be a no-op, not a
    # second fit racing the first for the bank.
    assert c.run_speculative_group_close() is False
    assert len(builds) == 1

    confirmed = _confirm_cloud(c)

    # No second fit — the confirm consumed the banked build…
    assert len(builds) == 1
    # …and it is the SAME candidate, not merely an equal one: the eager fit
    # buys latency, never a different product.
    assert c.candidate is banked
    assert confirmed["candidate_fingerprint"] == banked.fingerprint
    assert fakes.published_candidates == [banked]
    # The bank is spent, so a re-delivered signal still cannot fit twice.
    assert c._speculative_close is None
    assert c.confirm_cloud_measure_group() is None
    assert len(builds) == 1


def test_a_retake_discards_the_eager_fit_and_the_confirm_refits_the_new_cloud():
    """The retake contract, preserved through the rider (owner requirement).

    A voluntary retake of the final position (§2.6) means the cloud CHANGED,
    so anything fitted from the old one is answering a question nobody asked
    any more. The discard is atomic with the re-stash of the combine, which is
    what lets the confirm trust a bank without a generation counter to check
    it against — and it is what keeps T3's data contract true: the fit consumes
    exactly the accepted cloud as of the close.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    attempt = _walk_measure_cloud_to_accept(c)
    builds = _count_builds(c)

    assert c.run_speculative_group_close() is True
    stale = c._speculative_close.candidate
    assert len(builds) == 1

    # The household redoes the final spot rather than continuing.
    retake = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)
    assert retake["accepted"] is True
    # Still held, still theirs to end — a retake is not a confirmation.
    assert retake["awaiting_confirm"] is True
    assert c.cloud_measure_group_awaiting_confirm() is True
    # THE DISCARD: the stale build is gone, dropped in the same locked region
    # that re-stashed the new combine.
    assert c._speculative_close is None

    confirmed = _confirm_cloud(c)

    # The confirm REFITTED — it did not smuggle the pre-retake build through.
    assert len(builds) == 2
    assert c.candidate is not stale
    assert confirmed["candidate_fingerprint"] == c.candidate.fingerprint
    assert fakes.published_candidates == [c.candidate]


def test_an_eager_fit_failure_surfaces_on_the_confirm_not_before():
    """A speculative failure must not corrupt the confirm flow.

    The household has not asked for this computation yet and may still retake,
    which would moot it entirely — so a failure here renders NOTHING. The bank
    stays empty, the held window stays open, and the confirm refits and raises
    the identical error from the identical place it always did, where the host
    maps it to a real terminal screen.

    The cost is one wasted fit on a session that is already ending; the
    alternative — re-raising a stored exception across a thread boundary —
    buys seconds on a terminal path in exchange for a second failure route.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_accept(c)

    def _boom(_analysis, _cloud):
        raise RuntimeError("synthetic fit failure")

    c._build_candidate = _boom

    assert c.run_speculative_group_close() is False

    # NOTHING moved: no candidate, no publish, no failure screen, and the
    # household's retake window is exactly as open as before.
    assert c._speculative_close is None
    assert c.candidate is None
    assert fakes.published_candidates == []
    assert c.cloud_measure_group_awaiting_confirm() is True
    assert c.cloud_close_state == CLOUD_CLOSE_AWAITING_CONFIRM

    # It surfaces where it always did — on the household's own confirmation.
    with pytest.raises(RuntimeError, match="synthetic fit failure"):
        c.confirm_cloud_measure_group()

    # **THE DISCRIMINATOR for the decoupling itself**, and the only assertion
    # in the suite that can tell the two predicates apart. A close that RAISED
    # leaves ``_candidate`` unset — that is T3's retryability contract, still
    # intact below — so the pre-rider predicate (``self._candidate is None``)
    # would report this set as still awaiting confirmation and re-hold a
    # runner whose household already tapped Continue. Only a predicate that
    # asks "has the household confirmed?" gets it right: the window shuts on
    # the TAP, not on whether the fit behind it succeeded.
    assert c.cloud_measure_group_awaiting_confirm() is False
    assert c.candidate is None


def test_only_the_pre_apply_group_close_fires_a_candidate_across_a_whole_session():
    """The `phase == PHASE_CLOUD_MEASURE` guard in ``_close_cloud_group`` is
    load-bearing and gets its own pin: ``_close_cloud_group`` is shared by BOTH
    position groups, so without it the POST-apply cloud's close would build a
    second candidate — over an already-applied speaker, on evidence gathered
    through the correction it would be re-deriving.

    Re-derived for the two-stage world (work order D1/D2): the journey is two
    SESSIONS now, so this walks both — stage 1's ten captures plus its explicit
    confirmation, then stage 2's six against a fresh applied conductor — and
    asserts exactly one candidate across the pair, built by the confirmation
    and never by any capture verdict. The old single-conductor version could
    not express this at all once the index spaces split.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    stage1 = _cloud_conductor(fakes)

    attempt = 1
    for index in sorted(CLOUD_MAP):
        verdict = _run_phase(stage1, index, attempt)
        attempt += 1
        assert verdict["accepted"] is True, index
        assert "auto_apply" not in verdict, index
        assert stage1.candidate is None, index
    assert _confirm_cloud(stage1)["candidate_fingerprint"]
    assert len(fakes.published_candidates) == 1

    # STAGE 2, on its own conductor: applied, its own index space, its own
    # post-apply group. It must close that group and publish NOTHING.
    fakes.apply_done = True
    stage2 = _conductor(
        fakes,
        index_phase_map=STAGE2_MAP,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    attempt = 1
    for index in sorted(STAGE2_MAP):
        verdict = _run_phase(stage2, index, attempt)
        attempt += 1
        assert verdict["accepted"] is True, index
        assert "auto_apply" not in verdict, index

    assert len(fakes.published_candidates) == 1
    assert stage2.candidate is None
    # The post-apply group DID close — this is not a vacuous pass.
    assert PHASE_CLOUD_VERIFY in stage2.accepted_phases
    assert stage2.group_geometry(PHASE_CLOUD_VERIFY) is not None
    assert stage2.current_phase == PHASE_DONE


def test_a_session_with_no_cloud_group_still_builds_the_candidate_at_measure():
    """The pre-cloud 3-entry shape has nothing to wait for, so it must behave
    EXACTLY as it did before the timing move — same accept, same payload keys,
    same auto-apply timing. The rule is "the fit runs at the last capture
    before the apply", and for this shape that capture is MEASURE."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)  # the default {1: check, 2: measure, 3: verify}
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)

    assert verdict["accepted"] is True
    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict
    assert verdict["candidate_fingerprint"] == c.candidate.fingerprint
    assert len(fakes.published_candidates) == 1
    # No cloud exists, so no cloud evidence can ride the candidate — the
    # pre-move shape, byte for byte.
    assert c.candidate.exclusion_evidence == {}
    assert c.current_phase == PHASE_APPLYING


def test_the_clouds_honesty_verdict_reaches_the_fit_envelope():
    """THE wiring acceptance (plan PR-6, interpretation call (A)): the merged
    honesty mask a closed cloud produced actually binds the correction
    envelope, on the live path.

    A position-invariant comb cloud identifies real nulls; those intervals must
    (a) reach ``compose_envelope``'s ``spatial_exclusion_limit`` term, visible
    in the persisted fit's own per-octave reason summary, (b) cost the fit ALL
    correction depth inside them — zero gain spent where EQ cannot help — and
    (c) ride the candidate as the exclusion reason of record, with the τ/r
    registry that justifies them.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    fakes.verify = _comb_cloud_analysis_factory()
    c = _cloud_conductor(fakes)
    verdict = _walk_measure_cloud_to_close(c)
    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict

    pipeline = c.group_cloud_result(PHASE_CLOUD_MEASURE)
    assert pipeline["available"] is True
    registry = pipeline["null_registry"]
    assert registry["classification"] == "position_invariant"
    assert registry["nulls"], "the fixture must identify nulls to prove anything"
    intervals = [tuple(band) for band in pipeline["merged_excluded_bands_hz"]]
    assert intervals

    # (a) the term bound at least one octave of the driver that reaches these
    # frequencies — the fit's OWN persisted account of why.
    reasons = {
        reason
        for fit in c.candidate.linearization.values()
        for reason in fit["reason_summary"].values()
    }
    assert "envelope_limited_by_spatial_exclusion" in reasons

    # (b) no correction is placed inside an identified null. NOTE: on THIS
    # fixture this assertion does not discriminate — it holds in the severed
    # case too, because every filter the fit places lands over an octave and a
    # half below the lowest null the cloud identifies, so there was never a
    # filter up there to remove (PR-6a's own corpus acceptance records the same
    # shape — the exclusion punches holes rather than moving filters).
    #
    # Stated as a SEPARATION and not as two frequency ranges on purpose: the
    # TOP of the fit's range tracks the shared fixture's bump (the 150 Hz floor
    # is the woofer RoleBand's own edge and does not move), so the literal that
    # used to sit here ("150-1485 Hz") went stale the moment R10a moved that
    # bump to +3 dB at 2400 Hz. Re-derived at that revision on 2026-08-02, the
    # fit tops out near 2.4 kHz and the nulls start above 7 kHz — a margin of
    # ~1.6 octaves, i.e. the conclusion holds with room to spare rather than
    # by a hair. If a later fixture change narrows that, this note is the
    # thing to re-measure; the endpoints themselves are not the claim.
    #
    # It is kept as a standing invariant, not as this test's proof; (a) and
    # (c) plus the sibling severing test are what carry that.
    for fit in c.candidate.linearization.values():
        for biquad in fit["filters"]:
            for lo, hi in intervals:
                assert not (lo <= float(biquad["freq"]) <= hi), (biquad, (lo, hi))

    # (c) the reason of record rides the candidate — the same intervals, the
    # same registry, the cloud's own N.
    evidence = c.candidate.exclusion_evidence
    assert [tuple(b) for b in evidence["excluded_bands_hz"]] == intervals
    assert evidence["null_registry"]["nulls"] == registry["nulls"]
    assert evidence["n_positions"] == len(c.group_positions(PHASE_CLOUD_MEASURE))
    assert evidence["phase"] == PHASE_CLOUD_MEASURE
    assert [band["center_hz"] for band in evidence["band_spread"]]

    # (d) the ROOM layer's half of the same payload (issue #1787, plan RC1).
    # The validity floor and the gated spec curve previously existed only in
    # the retention-prunable session bundle, so once a bundle aged out the room
    # layer could not tell where this speaker's gated measurement stops being
    # trustworthy nor what its gated response is. Both are copied verbatim from
    # this group's own pipeline result — the same source cloud_measure.json
    # reads — so the two copies cannot disagree.
    assert evidence["validity_floor_hz"] == pipeline["validity_floor_hz"]
    assert evidence["gated_spec_curve"]["freqs_hz"] == pipeline["curve"]["freqs_hz"]
    assert (
        evidence["gated_spec_curve"]["magnitude_db"]
        == pipeline["curve"]["magnitude_db"]
    )
    assert evidence["gated_spec_curve"]["freqs_hz"], "the curve must be non-empty"


def test_severing_the_cloud_wiring_changes_the_fit(monkeypatch):
    """The "delete the input, the test must fail" half of the acceptance.

    Same cloud, same MEASURE analysis — but with ``_cloud_fit_evidence``
    severed the fit never learns what the cloud found, the exclusion term is
    absent from every reason summary, the fit's own permitted band is wider,
    and the candidate carries no reason of record. If a future edit quietly
    stopped threading the cloud into ``compose_envelope``, THIS is the state
    the passing test above would collapse into.

    **The emitted correction now differs too** (PR-L5). Until L5 the biquads
    and trims were IDENTICAL wired and severed on this fixture — the cut-only
    fit placed every filter over an octave and a half below the lowest null the
    cloud identifies (the sibling test's note (b) carries the measured
    separation and the reason it is not written here as two ranges), so the
    exclusion had no filter to move and only narrowed the permitted band. L5
    makes the cloud load-bearing on the FILTERS: boost permission is gated on
    the cloud verdict having reached the envelope, because without it
    ``allowed_depth_db`` is not zeroed in the registry's nulls and a lift could
    be designed into one. So the wired run emits a boost the severed run does
    not, and severing now costs the correction a filter rather than only a
    disclosure.
    """
    def _run(sever: bool):
        fakes = FakeSeams()
        fakes.measure = lambda program: _eligible_measure_analysis(program)
        fakes.verify = _comb_cloud_analysis_factory()
        c = _cloud_conductor(fakes)
        if sever:
            monkeypatch.setattr(c, "_cloud_fit_evidence", lambda combined: None)
        _walk_measure_cloud_to_close(c)
        return c.candidate

    wired = _run(sever=False)
    severed = _run(sever=True)

    wired_reasons = {
        reason for fit in wired.linearization.values()
        for reason in fit["reason_summary"].values()
    }
    severed_reasons = {
        reason for fit in severed.linearization.values()
        for reason in fit["reason_summary"].values()
    }
    assert "envelope_limited_by_spatial_exclusion" in wired_reasons
    assert "envelope_limited_by_spatial_exclusion" not in severed_reasons
    assert wired.exclusion_evidence and severed.exclusion_evidence == {}
    # The FIT differs, not only its disclosure: the cloud's exclusion narrows
    # the band the fit was permitted to work in. This is the assertion that
    # would fail if the wiring were reduced to a reporting-only change.
    wired_band = wired.linearization["tweeter"]["fit_band_hz"]
    severed_band = severed.linearization["tweeter"]["fit_band_hz"]
    assert wired_band != severed_band, (wired_band, severed_band)
    # PR-L5: and the emitted CORRECTION differs — the wired run was granted the
    # lift vocabulary, the severed run was not, so only the wired one can carry
    # a boost. This is the strongest form of "delete the input, the test must
    # fail": severing the cloud now costs a filter, not just a reason string.
    wired_boosts = [
        f for fit in wired.linearization.values() for f in fit["filters"]
        if f["gain"] > 0.0
    ]
    severed_boosts = [
        f for fit in severed.linearization.values() for f in fit["filters"]
        if f["gain"] > 0.0
    ]
    assert wired_boosts, "the wired run should have been granted boost"
    assert severed_boosts == [], severed_boosts
    # …and the cut-only skeleton underneath is still the same fit: severing
    # withholds the lift, it does not re-plan the correction.
    for role in sorted(wired.linearization):
        wired_cuts = [
            f for f in wired.linearization[role]["filters"] if f["gain"] <= 0.0
        ]
        severed_cuts = [
            f for f in severed.linearization[role]["filters"] if f["gain"] <= 0.0
        ]
        assert wired_cuts == severed_cuts, role


def test_abandoning_the_walk_before_the_group_closes_leaves_the_speaker_untouched():
    """The fail-safe direction of the timing move, stated as a property.

    An operator who walks away part-way through the prompted cloud never
    reaches the group close, so no candidate is built, no ``auto_apply`` is
    ever returned, and nothing is handed to the apply transaction — the
    speaker is exactly as it was. This is STRICTLY safer than the pre-move
    flow, where the apply fired at MEASURE and abandoning the walk left a
    household with a corrected speaker that was never verified.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    # Every prompted position except the last — then the operator stops.
    for index in CLOUD_MEASURE_INDEXES[:-1]:
        verdict = _run_phase(c, index, attempt)
        attempt += 1
        assert verdict["accepted"] is True
        assert "auto_apply" not in verdict

    assert c.candidate is None
    assert fakes.published_candidates == []
    assert PHASE_CLOUD_MEASURE not in c.accepted_phases
    # Nothing to apply, so the flow is still IN the cloud — never APPLYING.
    assert c.current_phase == PHASE_CLOUD_MEASURE


def test_a_group_close_with_no_retained_measure_analysis_fails_honestly():
    """The one state that could reach the group close without a fit input:
    a conductor carrying ``accepted_phases`` from a snapshot but none of the
    MEASURE analysis behind them — the same-session ``hydrate`` branch.

    **Production cannot construct it.** ``prepare_v2_session`` hydrates
    against a freshly MINTED capture session id, so ``snapshot.session_id ==
    session_id`` is never true there and hydrate always takes the
    fresh-start-at-CHECK branch (§5.6). This pins what happens if that ever
    stops being true: an honest raise (the host maps it to
    ``internal_error``, a real terminal screen) rather than a silent confirm
    with no ``auto_apply``, which would leave VERIFY's ``on_apply`` hold
    waiting on an apply that can never come.

    Since flow-simplification §2.6 the raise lands on the CONFIRM rather than
    on the final position's capture — the fit moved, the honesty did not.
    """
    fakes = FakeSeams()
    c = _cloud_conductor(fakes, accepted_phases=(PHASE_CHECK, PHASE_MEASURE))
    assert c.current_phase == PHASE_CLOUD_MEASURE

    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], 1)
    assert _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)["accepted"] is True
    with pytest.raises(CrossoverV2FlowError, match="no retained MEASURE analysis"):
        c.confirm_cloud_measure_group()


def test_a_candidate_build_failure_leaves_the_group_journalled_but_unaccepted(caplog):
    """N1: the exact forensic state a candidate-build raise leaves behind.

    ``_close_cloud_group``'s wrap protects the diagnostic PIPELINE, not the
    candidate build — the build is the session's product and is allowed to
    fail. Since flow-simplification §2.6 split the two, the split is visible
    here: the CAPTURES all succeeded and the group is genuinely accepted, and
    it is the household's CONFIRM — the fit — that raises. The host maps that
    to ``internal_error``; nothing durable claims a candidate.

    Pinned so nobody later reads the wrap's "the accept is already decided"
    comment as a promise it does not make.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)

    def _boom(_candidate):
        raise RuntimeError("synthetic publish-seam failure")

    c = _cloud_conductor(fakes)
    c._seams = replace(c._seams, publish_candidate=_boom)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)

    assert _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)["accepted"] is True
    with pytest.raises(RuntimeError, match="synthetic publish-seam failure"):
        c.confirm_cloud_measure_group()

    assert "event=correction.crossover_v2_cloud_group_complete" in caplog.text
    assert c.group_geometry(PHASE_CLOUD_MEASURE) is not None
    # The WALK completed and is recorded as such; only the fit failed.
    assert PHASE_CLOUD_MEASURE in c.accepted_phases
    # No half-published candidate is left readable on the conductor either:
    # the seam raised before it could be handed anywhere, and the fingerprint
    # never reached a verdict payload.
    assert fakes.published_candidates == []


def test_a_failed_cloud_pipeline_fits_without_cloud_terms_and_says_so(
    monkeypatch, caplog,
):
    """Honest degradation, named at the site: a group whose honesty pipeline
    never became available hands the fit NO cloud evidence — not the screen's
    intervals alone.

    That all-or-nothing rule is the wiring contract (issue #1742 item 4): the
    screen structurally cannot see a position-invariant null, so a screen-only
    mask would exclude the interference the cloud CAN see while silently
    correcting the interference it cannot. The session still produces a
    candidate and still auto-applies — a diagnostic failure is not a
    measurement failure — and the fallback is logged rather than silent.
    """
    import jasper.active_speaker.crossover_v2_flow as flow

    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    monkeypatch.setattr(
        flow, "assemble_cloud_group_result",
        lambda *a, **k: {"available": False, "reason": "pipeline_failed"},
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    fakes.verify = _comb_cloud_analysis_factory()
    c = _cloud_conductor(fakes)
    verdict = _walk_measure_cloud_to_close(c)

    assert verdict["accepted"] is True
    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict
    assert c.candidate is not None
    assert c.candidate.exclusion_evidence == {}
    assert "event=correction.crossover_v2_fit_without_cloud" in caplog.text
    assert "reason=pipeline_failed" in caplog.text


def test_a_cloud_pipeline_exception_never_costs_the_group_its_accept(monkeypatch):
    """S4 review finding (2026-07-26): the honest-instrument pipeline is
    diagnostic/disclosure machinery layered on TOP of an ALREADY-DECIDED
    accept — a bug in ``assemble_cloud_group_result`` (or the
    ``publish_cloud`` seam) must never flip that decision.
    ``_close_cloud_group``'s own wrap around ``_run_cloud_pipeline`` is the
    structural guarantee; this proves it holds even for a raise OUTSIDE
    ``assemble_cloud_group_result``'s own try/except (a genuinely unexpected
    pipeline bug, not the bounded family it already handles internally).
    """
    import jasper.active_speaker.crossover_v2_flow as flow

    def _boom(*_a, **_k):
        raise RuntimeError("synthetic pipeline bug")

    monkeypatch.setattr(flow, "assemble_cloud_group_result", _boom)

    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)

    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)

    assert verdict["accepted"] is True
    assert verdict["group_complete"] == PHASE_CLOUD_MEASURE
    assert PHASE_CLOUD_MEASURE in c.accepted_phases
    # The geometry verdict (PR-3b's own field, decided BEFORE the pipeline
    # ever runs) is unaffected either way.
    assert c.group_geometry(PHASE_CLOUD_MEASURE) is not None
    # The pipeline result is honestly None ("never successfully ran"), not a
    # fabricated availability of any kind.
    assert c.group_cloud_result(PHASE_CLOUD_MEASURE) is None


def test_an_unnamed_exception_family_still_propagates_through_the_outer_wrap(
    monkeypatch,
):
    """N1 review finding (2026-07-27): ``_close_cloud_group``'s own comment
    used to claim its outer wrap around ``_run_cloud_pipeline`` made the
    "pipeline exception cannot cost the accept" invariant "structurally true
    rather than merely usually true" — unconditionally. It is not: the wrap
    only catches the same six named types
    (OSError, RuntimeError, TypeError, ValueError, IndexError, AttributeError)
    ``assemble_cloud_group_result``'s own docstring discloses.
    ``test_a_cloud_pipeline_exception_never_costs_the_group_its_accept``
    (immediately above) proves a NAMED family (``RuntimeError``) is caught;
    this proves the complementary residual — a ``KeyError``, outside that
    family, is NOT caught here either and propagates straight through
    ``_close_cloud_group``, costing the group its accept (no ``PhaseVerdict``
    is ever returned; the whole ``consume_capture`` call raises).
    """
    import jasper.active_speaker.crossover_v2_flow as flow

    def _boom(*_a, **_k):
        raise KeyError("synthetic unnamed-family pipeline bug")

    monkeypatch.setattr(flow, "assemble_cloud_group_result", _boom)

    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)

    with pytest.raises(KeyError):
        _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)


def test_close_cloud_group_calls_the_combiner_exactly_once(monkeypatch):
    """S3 review finding, 2026-07-26 (timing sanity). The round-1 draft of
    this wiring called :func:`combine_cloud_positions` TWICE per group close
    — once for the retry-gating verdict via the old ``cloud_geometry_verdict``
    seam, once more from the honest-instrument pipeline. The two calls were
    byte-for-byte identical, but measured at 5.6-6.2 s per call on a laptop
    (interpreter-bound ``smooth_fractional_octave``), so the second call was
    pure operator wait with no evidentiary value — the fix (``_close_cloud_
    group`` combines once and both consumers read the same ``combined``
    object) is what this test pins.

    Wraps the REAL combiner (unlike ``_lock`` below, which stubs out
    ``_geometry_verdict_from_combined`` entirely) so the call COUNT is the
    only thing under test; the wrapped function still returns the genuine
    combined result, so the rest of the group-close path (geometry verdict,
    pipeline result) runs exactly as it would in production.
    """
    import jasper.active_speaker.crossover_v2_flow as flow

    calls: list[int] = []
    real_combine = flow.combine_cloud_positions

    def _counting_combine(positions):
        calls.append(len(positions))
        return real_combine(positions)

    monkeypatch.setattr(flow, "combine_cloud_positions", _counting_combine)

    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)

    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)

    assert verdict["accepted"] is True
    assert verdict["group_complete"] == PHASE_CLOUD_MEASURE
    # The group-end combine ran exactly ONCE for this close — not once for
    # the retry gate and again for the pipeline.
    assert len(calls) == 1
    assert calls[0] == len(CLOUD_MEASURE_INDEXES)


def test_cloud_position_retry_budget_is_per_position_not_per_group():
    """Eight prompted positions are eight independent captures. Collapsing them
    onto the phase's cumulative counter would let retakes early in a group
    refuse a later position that has not failed at all."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)

    first, second = CLOUD_MEASURE_INDEXES[0], CLOUD_MEASURE_INDEXES[1]
    fakes.verify = lambda program: _verify_analysis(program, locate_confidence=0.0)
    verdict = _run_phase(c, first, attempt)
    attempt += 1
    assert verdict["accepted"] is False
    fakes.verify = _verify_analysis
    _run_phase(c, first, attempt)  # the retake at the SAME index is admitted
    attempt += 1
    # ... and the NEXT position starts with a clean budget, not the previous
    # position's spent one.
    c.authorize_begin(second, attempt)
    assert c.armed_capture == (second, attempt)


def test_cloud_position_qc_rejects_a_capture_with_no_usable_summed_response():
    """Per-position work is light — but not absent: a position that yielded no
    curve is not evidence, so it is retaken rather than combined."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    index = CLOUD_MEASURE_INDEXES[0]

    fakes.verify = lambda program: replace(
        _verify_analysis(program), summed_response=None,
    )
    verdict = _run_phase(c, index, attempt)
    assert verdict["accepted"] is False
    assert c.group_positions(PHASE_CLOUD_MEASURE) == ()


def test_geometry_locked_group_asks_for_wider_retakes_then_proceeds(monkeypatch):
    """`geometry.locked` is the one actionable thing the geometry instrument can
    say ("spread the mic further"), so the group asks — twice at most, then
    proceeds with the verdict disclosed. Unbounded retrying against a
    source-fixed defect would never terminate, because no mic move decorrelates
    a null that does not move."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    _lock(monkeypatch)

    prompts = []
    for _ in range(GEOMETRY_RETRY_POSITIONS):
        verdict = _run_phase(c, last, attempt)
        attempt += 1
        assert verdict["accepted"] is False
        assert verdict["code"] == REASON_CLOUD_GEOMETRY_LOCKED
        prompts.append(verdict["prompt"])
        assert PHASE_CLOUD_MEASURE not in c.accepted_phases
        # The too-close take leaves the cloud — that is what a RETAKE is, the
        # only lever the fixed-length runner offers. Not a claim that dropping
        # beats appending: that claim was withdrawn in review (appending fills
        # the null further), so this asserts the mechanism, not a merit.
        assert last not in {
            int(pid.rsplit("_", 1)[1])
            for pid in c.group_positions(PHASE_CLOUD_MEASURE)
        }
    # Two rungs, so the second ask is a different instruction, not a repeat.
    assert prompts == list(CLOUD_GEOMETRY_RETRY_PROMPTS[:GEOMETRY_RETRY_POSITIONS])
    assert len(set(prompts)) == len(prompts)

    # Bounded: the third take is ACCEPTED even though geometry is still locked,
    # with the verdict disclosed rather than the household stuck.
    verdict = _run_phase(c, last, attempt)
    assert verdict["accepted"] is True
    assert verdict["geometry"]["locked"] is True
    assert c.group_geometry(PHASE_CLOUD_MEASURE)["locked"] is True
    assert PHASE_CLOUD_MEASURE in c.accepted_phases


def test_two_geometry_asks_leave_one_household_retry_in_the_pooled_budget(
    monkeypatch,
):
    """Two speaker asks spend two pooled extras; the third remains household.

    There is no geometry discount and no separate quality-failure budget.
    The planned close asks for the first wider take; that rejection asks for
    the second. Those two conductor-initiated extras leave exactly one of the
    position's three pooled extras for the household after an ordinary locate
    miss.
    """
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    _lock(monkeypatch)

    # Two geometry retakes — good captures, wider spots.
    for _ in range(GEOMETRY_RETRY_POSITIONS):
        assert _run_phase(c, last, attempt)["code"] == REASON_CLOUD_GEOMETRY_LOCKED
        attempt += 1

    # Now ONE ordinary failure at that same position. It lands on the second
    # speaker-booked extra and asks for the sole remaining household extra.
    monkeypatch.undo()
    fakes.verify = lambda program: _verify_analysis(program, locate_confidence=0.0)
    verdict = _run_phase(c, last, attempt)
    attempt += 1
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_LOCATE_FAILED
    assert verdict["attempts"] == {
        "used": 2,
        "allowed": flow.MAX_EXTRA_ATTEMPTS_PER_POSITION,
        "left": 1,
        "by_speaker": 2,
        "by_household": 0,
    }

    # ...and the final pooled extra is the household's retry.
    fakes.verify = _verify_analysis
    verdict = _run_phase(c, last, attempt)
    assert verdict["accepted"] is True
    assert verdict["attempts"]["by_speaker"] == 2
    assert verdict["attempts"]["by_household"] == 1
    assert verdict["attempts"]["left"] == 0
    assert PHASE_CLOUD_MEASURE in c.accepted_phases


def test_a_spent_cloud_position_is_attributed_and_the_group_continues(
    monkeypatch, caplog,
):
    """The pooled bound is finite, and hitting it does NOT kill the session.

    Transformed from ``test_the_geometry_discount_is_capped_and_still_refuses_a_runaway``,
    which pinned the behaviour the owner ruled against (#2086): the slot's
    budget "still bites" with a terminal ``CaptureBeginRefused`` raised BEFORE
    any audio plays, while the phone's screen still said "try again". The bound
    is still finite — that half of the old test is what this keeps — but the
    fourth failure now settles the position instead of the session.
    """
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    attempt = _walk(c, (1, 2), 1)
    index = CLOUD_MEASURE_INDEXES[0]

    # A non-terminal position (not the group's last) can only fail on quality.
    fakes.verify = lambda program: _verify_analysis(
        program, locate_confidence=0.0, pilot_snr_ok=True,
    )
    # The planned capture plus two extras: still ordinary retries.
    for extra in (0, 1, 2):
        verdict = _run_phase(c, index, attempt)
        attempt += 1
        assert verdict["accepted"] is False
        assert verdict["attempts"]["used"] == extra

    # The last extra. FINITE — the flow stops asking — and honest: the position
    # is marked unresolved carrying the observed condition, and the group
    # advances rather than the session dying at the microphone.
    verdict = _run_phase(c, index, attempt)
    attempt += 1
    assert verdict["accepted"] is True
    assert verdict["unresolved"] == {
        "index": index,
        "code": REASON_LOCATE_FAILED,
        "diagnosis": locate_failed_diagnosis(True),
    }
    assert verdict["attempts"]["left"] == 0
    spent = [
        record.getMessage() for record in caplog.records
        if "crossover_v2_position_attempts_spent" in record.getMessage()
    ]
    assert len(spent) == 1
    assert f'diagnosis="{locate_failed_diagnosis(True)}"' in spent[0]
    assert "pilot_heard=true" in spent[0]
    assert "observed=locate_failed" in spent[0]
    # Nothing was retained for it — an unresolved position is not evidence.
    assert index not in {
        int(pid.rsplit("_", 1)[1])
        for pid in c.group_positions(PHASE_CLOUD_MEASURE)
    }
    # …and the NEXT prompted position is admitted normally, with its own budget.
    second = CLOUD_MEASURE_INDEXES[1]
    c.authorize_begin(second, attempt)
    assert c.armed_capture == (second, attempt)


