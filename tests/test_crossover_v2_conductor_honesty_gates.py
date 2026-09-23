# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: measurement-honesty gates, alignment/phase wiring, and measured completion."""

from __future__ import annotations

import math
import types
import pytest
from dataclasses import replace
from jasper.active_speaker.crossover_v2.diagnostics import PILOT_SNR_UNUSABLE_DB, _worst_pilot_snr_db
from jasper.active_speaker.crossover_v2.planning import alignment_to_candidate_fields
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    ALIGNMENT_OK,
    SegmentLocation,
)
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginRefused
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY
from tests.crossover_v2_fixtures import (
    FakeSeams,
    _alignment,
    _check_analysis,
    _conductor,
    _run_phase,
    _snr_analysis,
    _snr_pilot,
)


@pytest.mark.parametrize("refused", [False, True])
@pytest.mark.parametrize("charge", ["operator", "speaker"])
def test_executor_admission_uses_only_the_runs_pose_ledger(refused, charge):
    from jasper.active_speaker.crossover_v2.admission import SlotAttempts
    from jasper.active_speaker.crossover_v2.refusal_copy import NON_RETRIABLE_CODES

    conductor = _conductor(FakeSeams())
    ledger = SlotAttempts(charge=charge, retries_per_pose=0 if charge == "speaker" else 1)
    conductor.authorize_begin(1, 1, executor_ledger=ledger)
    assert ledger.admitted == 1
    if refused:
        conductor._last_reason[conductor._slot_of_index(1)] = next(iter(NON_RETRIABLE_CODES))
        with pytest.raises(CaptureBeginRefused):
            conductor.authorize_begin(1, 2, executor_ledger=ledger)
    else:
        conductor.authorize_begin(1, 2, executor_ledger=ledger)
    assert ledger.by_household == (1 if charge == "operator" and not refused else 0)
    assert ledger.by_speaker == (1 if charge == "speaker" and not refused else 0)
    assert ledger.admitted == (1 if refused else 2)
    assert conductor._slot_attempts == {}


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
    from jasper.active_speaker.crossover_v2.capture_dispatch import (  # lazy: avoid measurement-stack import cost on unused paths
        _stimulus_locate_ok,
    )

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


def test_check_agc_and_snr_and_channel_map_verdicts():
    # linearity=False with ambient looking clean (snr_floor_ok defaults True)
    # ⇒ the phone's own AGC is the honest cause.
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, linearity=False)
    c = _conductor(fakes)
    assert _run_phase(c, 1, 1).fault == "agc_behavioral_fail"

    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, snr_floor_ok=False, pilot_snr_ok=True)
    c = _conductor(fakes)
    assert _run_phase(c, 1, 1).fault == "snr_floor"

    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, channel_map=False)
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict.fault == "channel_map_mismatch"
    assert REASON_REGISTRY[verdict.fault].template == "hard_stop"
    # Hard stop: budget 0 ⇒ the very next begin is refused.
    c._last_reason["check"] = verdict.fault
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
    assert verdict.fault == "snr_floor"
    assert REASON_REGISTRY[verdict.fault].template == "fix_and_retry"


def test_check_without_ambient_still_requires_a_level_solution():
    fakes = FakeSeams()
    fakes.check = lambda program: replace(
        _check_analysis(program, linearity=None, channel_map=None, snr_floor_ok=False),
        ambient_report={"bands": []},
    )
    verdict = _run_phase(_conductor(fakes), 1, 1)
    assert verdict.ok is False
    assert verdict.fault == "snr_floor"
    assert verdict.evidence["pilot_ambient"] == "unavailable"
    assert verdict.capabilities["level_solve"] is False
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
    assert verdict.fault == "noisy_room_linearity"
    assert REASON_REGISTRY[verdict.fault].template == "fix_and_retry"


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
