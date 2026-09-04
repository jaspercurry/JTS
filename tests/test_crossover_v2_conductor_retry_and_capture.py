# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: the bounded-retry ruling and the capture-plan auto-advance policy."""

from __future__ import annotations

import pytest
from dataclasses import replace
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_VERIFY
from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_ENTRY_BASELINE,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_CORRECTION_MODEL_ERROR,
    REASON_CLOUD_GEOMETRY_LOCKED,
    REASON_LOCATE_FAILED,
    REASON_REGISTRY,
    locate_failed_diagnosis,
)
from jasper.active_speaker.crossover_v2_flow import (
    AUTO_ADVANCE_COUNTDOWN,
    AUTO_ADVANCE_COUNTDOWN_S,
    AUTO_ADVANCE_ON_APPLY,
    AUTO_ADVANCE_TAP,
    CLOUD_GEOMETRY_RETRY_PROMPTS,
    CLOUD_POSITION_PROMPTS,
    DEFAULT_CLOUD_MEASURE_POSITIONS,
    GEOMETRY_RETRY_POSITIONS,
    POSITION_ROLES,
    CrossoverV2Session,
    CrossoverV2FlowError,
    build_v2_capture_plan,
    build_v2_cloud_index_phase_map,
    cloud_capture_target,
)
from jasper.audio_measurement import gating
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginRefused
from tests.crossover_v2_fixtures import (
    CAPS,
    bank_into,
    CLOUD_MAP,
    CLOUD_MEASURE_INDEXES,
    CLOUD_VERIFY_INDEXES,
    FC_HZ,
    FakeSeams,
    SESSION,
    SESSION_VOLUME_DB,
    SHORT_VERIFY_CLOUD_INDEXES,
    SHORT_VERIFY_MAP,
    STAGE2_MAP,
    VERIFY_INDEX,
    _check_analysis,
    _cloud_conductor,
    _conductor,
    _lock,
    _measure_analysis,
    _preset,
    _roles,
    _run_phase,
    _verify_analysis,
    _walk,
)


# ===========================================================================
# The bounded-retry ruling (owner, 2026-08-03, issue #2086). One prompted
# position gets the planned capture plus THREE extra attempts, pooled across
# everyone who can ask for one; exhaustion attributes and degrades rather than
# killing the session with copy that says "try again".
# ===========================================================================


def test_every_retriable_reason_has_one_structured_diagnosis_source():
    """Exhaustive negative guard for the count-only regression.

    Every retriable registry row must carry a diagnosis, and its historical
    retryable message/banner must be composed from that same value. Adding a
    new retriable code as a bare literal fails here before exhaustion can ship
    generic count-only copy for it.
    """
    retriable = {
        code: spec for code, spec in REASON_REGISTRY.items()
        if spec.retry_budget > 0
    }
    assert retriable
    for code, spec in retriable.items():
        assert spec.retry_copy is not None, code
        assert (spec.message or spec.banner) == spec.retry_copy.message, code
        assert flow.reason_diagnosis(code, spec), code


@pytest.mark.parametrize(
    ("analysis_kwargs", "expected_code"),
    [
        ({"linearity": False}, refusal_copy.REASON_AGC_BEHAVIORAL_FAIL),
        ({"pilot_snr_ok": False}, refusal_copy.REASON_SNR_FLOOR),
    ],
)
def test_non_special_reasons_keep_their_diagnosis_on_the_final_extra(
    analysis_kwargs, expected_code,
):
    """Representative literal reasons terminate with X, never count alone."""
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, **analysis_kwargs)
    c = _conductor(fakes)

    for attempt in range(1, flow.MAX_EXTRA_ATTEMPTS_PER_POSITION + 2):
        verdict = _run_phase(c, 1, attempt)

    diagnosis = flow.reason_diagnosis(
        expected_code, REASON_REGISTRY[expected_code]
    )
    assert verdict["code"] == expected_code
    assert verdict["terminal"] is True
    assert verdict["reason"].startswith(diagnosis)
    assert "try again" not in verdict["reason"].lower()
    assert "cannot continue" in verdict["reason"].lower()


def test_verify_inconclusive_keeps_its_measured_reflection_at_exhaustion():
    """#2095 evidence and #2097 terminal action stay on the same capture."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    fakes.verify = lambda program: _verify_analysis(
        program,
        max_db=0.5,
        gate_ms=5.0,
        floor_source=gating.FLOOR_MEASURED,
    )

    for attempt in range(3, 3 + flow.MAX_EXTRA_ATTEMPTS_PER_POSITION + 1):
        verdict = _run_phase(c, 3, attempt)

    diagnosis = refusal_copy.verify_inconclusive_diagnosis(True)
    assert verdict["terminal"] is True
    assert verdict["reflection_measured"] is True
    assert verdict["reason"].startswith(diagnosis)
    assert "try again" not in verdict["reason"].lower()


def test_the_extra_try_bound_is_pooled_across_initiators(monkeypatch):
    """Ruling item 1 + 4, replayed on the shape that killed the 2026-08-03
    verify: at position index 6 the flow spent locate_failed, two geometry
    rungs, then locate_failed again — five attempts at one spot, because each
    reason code held its own budget and the geometry discount forgave two more.
    The sixth begin was refused pre-play.

    One pooled meter now covers all of it. The bound is shared (a geometry rung
    spends an extra like anything else), and the accounting is not (it is
    booked to the speaker, because the speaker is who asked)."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    _lock(monkeypatch)

    # The planned capture, then the wider retake the speaker asks for.
    for _ in range(GEOMETRY_RETRY_POSITIONS):
        verdict = _run_phase(c, last, attempt)
        attempt += 1
        assert verdict["code"] == REASON_CLOUD_GEOMETRY_LOCKED
    assert verdict["attempts"]["by_speaker"] == 1
    assert verdict["attempts"]["by_household"] == 0

    # The geometry ladder is spent; ordinary quality failures follow.
    monkeypatch.undo()
    fakes.verify = lambda program: _verify_analysis(
        program, locate_confidence=0.0, pilot_snr_ok=True,
    )
    verdict = _run_phase(c, last, attempt)
    attempt += 1
    assert verdict["code"] == REASON_LOCATE_FAILED
    # The take the speaker asked for is still the speaker's ask.
    assert verdict["attempts"] == {
        "used": 2, "allowed": 3, "left": 1, "by_speaker": 2, "by_household": 0,
    }

    # The household's own try is the third and last extra.
    verdict = _run_phase(c, last, attempt)
    attempt += 1
    assert verdict["attempts"] == {
        "used": 3, "allowed": 3, "left": 0, "by_speaker": 2, "by_household": 1,
    }
    # FINITE and honest: the position carries the condition actually observed,
    # and the group closes with what it has instead of the session dying.
    assert verdict["accepted"] is True
    assert verdict["unresolved"] == {
        "index": last,
        "code": REASON_LOCATE_FAILED,
        "diagnosis": locate_failed_diagnosis(True),
    }
    assert verdict["group_complete"] == PHASE_CLOUD_MEASURE
    assert PHASE_CLOUD_MEASURE in c.accepted_phases


def test_an_accepted_capture_leaves_the_positions_extras_intact():
    """Ruling item 4. A position measured cleanly on its planned take has spent
    nothing, so a household that chooses to redo it gets the full three tries.

    This is the compounding defect from #2086: acceptance popped the reason but
    left the cumulative counter standing, so ONE voluntary retake of a healthy
    position landed in a meter with zero headroom and the next begin killed the
    session. Here the retakes all fail and the session survives — the earlier
    take was never lost, which is what makes giving up on it safe."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    index = CLOUD_MEASURE_INDEXES[0]

    verdict = _run_phase(c, index, attempt)
    attempt += 1
    assert verdict["accepted"] is True
    assert verdict["attempts"]["left"] == 3, "an accepted take consumes no extra"

    fakes.verify = lambda program: _verify_analysis(program, locate_confidence=0.0)
    for extra in (1, 2):
        verdict = _run_phase(c, index, attempt)
        attempt += 1
        assert verdict["accepted"] is False
        assert verdict["attempts"]["by_household"] == extra

    # The third failed retake settles the slot — and because the ORIGINAL take
    # is still retained, nothing is unresolved: the earlier measurement stands.
    verdict = _run_phase(c, index, attempt)
    attempt += 1
    assert verdict["accepted"] is True
    assert verdict["kept_earlier_take"] is True
    assert "unresolved" not in verdict
    assert index in {
        int(pid.rsplit("_", 1)[1])
        for pid in c.group_positions(PHASE_CLOUD_MEASURE)
    }


def test_a_group_that_cannot_reach_the_floor_ends_honestly_not_with_retry_copy():
    """Ruling item 3's second half. When the phase genuinely cannot proceed the
    session does end — but the copy names the tries that were spent, never an
    action the flow will refuse. The pre-play refusal whose screen said "measure
    again" is the exact shape the owner ruled out."""
    fakes = FakeSeams()
    fakes.apply_done = True
    # A one-position verify group: giving its only position up would leave zero
    # curves, which is below MIN_RESOLVED_CLOUD_POSITIONS with nothing left to
    # walk, so this is the honest-terminal branch.
    c = _conductor(
        fakes,
        index_phase_map=SHORT_VERIFY_MAP,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    index = SHORT_VERIFY_CLOUD_INDEXES[0]
    attempt = _walk(c, (1,), 1)

    fakes.verify = lambda program: _verify_analysis(
        program, locate_confidence=0.0, pilot_snr_ok=True,
    )
    for _ in range(flow.MAX_EXTRA_ATTEMPTS_PER_POSITION + 1):
        verdict = _run_phase(c, index, attempt)
        attempt += 1
        assert verdict["accepted"] is False
    assert verdict["attempts"]["left"] == 0
    # The final capture itself is terminal — no retry screen/button survives
    # until a doomed next begin — and the group did NOT close: there is no
    # cloud to close with.
    assert verdict["terminal"] is True
    assert verdict["terminal_outcome"] == "below_position_floor"
    assert verdict["reason"].startswith(locate_failed_diagnosis(True))
    assert "try again" not in verdict["reason"].lower()
    assert "too few positions" in verdict["reason"].lower()
    assert PHASE_CLOUD_VERIFY not in c.accepted_phases

    # Defensive replay backstop remains diagnosis-identical.
    with pytest.raises(CaptureBeginRefused) as excinfo:
        c.authorize_begin(index, attempt)
    assert excinfo.value.code == REASON_LOCATE_FAILED, "attribute the observation"
    assert "3 extra tries" in excinfo.value.user_message
    assert excinfo.value.user_message.startswith(
        locate_failed_diagnosis(True)
    )
    assert "too few positions" in excinfo.value.user_message.lower()


def test_a_spent_final_slot_terminalizes_its_close_time_refusal():
    """The cloud-close hard stop replaces, rather than hides behind, X.

    The last verify-cloud position spends its pooled extras on locate misses.
    The group can still close without that spot, but its delta probe then
    refuses with ``correction_model_error``. That closing finding is the final
    truth: publish its exact code/copy as terminal on THIS capture, never the
    earlier locate diagnosis plus a retry the ledger cannot admit.
    """
    fakes = FakeSeams()
    fakes.apply_done = True
    c = _conductor(
        fakes,
        index_phase_map=STAGE2_MAP,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    # Isolate the close seam under test from delta-probe AND round-grading
    # arithmetic; the real classifier's mapping/copy is independently
    # exhaustive below. Injected at ``_grade_round_once`` rather than at the
    # probe's own seam because the fifth-principle routing deleted that seam:
    # a close-time refusal is now the ROUND's answer, and this is where it
    # enters the close.
    c._grade_round_once = (  # type: ignore[method-assign]
        lambda verdict: (
            flow.PhaseVerdict(False, REASON_CORRECTION_MODEL_ERROR)
            if c.current_phase == PHASE_CLOUD_VERIFY
            else verdict
        )
    )

    attempt = _walk(c, (VERIFY_INDEX, *CLOUD_VERIFY_INDEXES[:-1]), 1)
    last = CLOUD_VERIFY_INDEXES[-1]
    fakes.verify = lambda program: _verify_analysis(
        program, locate_confidence=0.0, pilot_snr_ok=True,
    )
    for _ in range(flow.MAX_EXTRA_ATTEMPTS_PER_POSITION + 1):
        verdict = _run_phase(c, last, attempt)
        attempt += 1

    closing_copy = REASON_REGISTRY[REASON_CORRECTION_MODEL_ERROR].message
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_CORRECTION_MODEL_ERROR
    assert verdict["reason"] == closing_copy
    assert verdict["terminal"] is True
    assert verdict["terminal_outcome"] == "phase_cannot_proceed"
    assert verdict["attempts"]["left"] == 0
    assert "unresolved" not in verdict
    assert "could hear the speaker" not in verdict["reason"]
    assert "previous sound has been put back" in verdict["reason"]


def test_no_exhaustion_refusal_ever_carries_a_reasons_try_again_copy():
    """The ruling's hard prohibition, pinned over the WHOLE registry rather than
    one code: a refusal reached by spending a position's extras must never
    publish the reason's own action sentence, because every retriable one of
    those ends by inviting a retry the flow will not grant.

    Mutation-checked: reverting ``authorize_begin``'s exhaustion arm to the old
    ``raise CaptureBeginRefused(spec.code, spec.message or spec.banner)`` fails
    this. The message is taken from a REAL refusal rather than from the
    formatter, because a test that only inspects the formatter passes happily
    while the refusal publishes something else entirely."""
    retriable = [
        code for code in REASON_REGISTRY
        if code not in flow.NON_RETRIABLE_CODES
    ]
    assert retriable, "fixture sanity: the registry has retriable codes"

    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(program, locate_confidence=0.01)
    c = _conductor(fakes)
    for attempt in range(1, flow.MAX_EXTRA_ATTEMPTS_PER_POSITION + 2):
        assert _run_phase(c, 1, attempt)["accepted"] is False
    with pytest.raises(CaptureBeginRefused) as excinfo:
        c.authorize_begin(1, flow.MAX_EXTRA_ATTEMPTS_PER_POSITION + 2)
    published = excinfo.value.user_message

    assert "try again" not in published.lower()
    assert "measure again" not in published.lower()
    for code in retriable:
        spec = REASON_REGISTRY[code]
        assert published != (spec.message or spec.banner), (
            f"{code}: an exhaustion refusal must not republish retry copy"
        )


def test_thin_evidence_lock_is_disclosed_not_retried(monkeypatch):
    """``thin_evidence`` marks a verdict resting on the bare minimum usable echo
    estimates — a cliff, not a gradient (GeometryLock's own docstring). Spending
    two more prompted positions on that basis buys a verdict the instrument
    already qualifies, so a thin lock is accepted and disclosed."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    _lock(monkeypatch, thin=True)

    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)
    assert verdict["accepted"] is True
    assert verdict["geometry"]["locked"] is True
    assert verdict["geometry"]["thin_evidence"] is True
    assert PHASE_CLOUD_MEASURE in c.accepted_phases


def test_the_three_unprompted_phases_each_bank_a_take_of_their_own():
    """CHECK, MEASURE and VERIFY produce a banked take, like every other phase.

    Before this they produced none at all: their arms were the three the
    dispatch handed no ``index``, no ``attempt`` and no ``result``, so there
    was no identity to bank one under and no bytes to bank. Offline analyze
    could see a session's positions and its baseline and simply not its
    CHECK, its MEASURE or its VERIFY.

    What is banked is the CAPTURE — the digest, the identity, and the complex
    responses it measured — because that is what makes an offline replay of
    these phases possible at all. Their VERDICTS are not duplicated into the
    take: those live where each phase already puts them, and are rewritten
    inside a round that a take outlives. The curves are pinned separately, by
    ``test_every_banked_kind_carries_the_phase_its_analysis_measured``.
    """
    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(fakes.seams(), bank_take=bank_into(retained)),
        index_phase_map=CLOUD_MAP,
    )
    _walk(c, (1, 2), 1)

    # VERIFY is stage 2's, so it takes the stage-2 shape to reach.
    verify_retained: list = []
    verify_fakes = FakeSeams()
    stage2 = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(
            verify_fakes.seams(), bank_take=bank_into(verify_retained),
        ),
        index_phase_map=STAGE2_MAP,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    _run_phase(stage2, VERIFY_INDEX, 1)

    # The LIST, not a phase-keyed dict: one take per accepted capture, and a
    # dict would quietly collapse a double-bank into the single entry this pin
    # was looking for — the real store would not catch it either, because two
    # banks of one record are byte-identical and therefore idempotent.
    all_banked = retained + verify_retained
    assert [meta["phase"] for meta in all_banked] == [
        PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY,
    ]
    banked = {meta["phase"]: meta for meta in all_banked}
    # Every one carries the identity a replay resolves it by, and the digest
    # that verifies the bytes it finds.
    for phase in (PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY):
        take = banked[phase]
        assert take["session_id"] == SESSION
        assert take["index"] > 0
        assert take["attempt"] > 0
        assert take["wav_sha256"]
        assert take["captured_at"]


def _phase_probe(analysis):
    """The same analysis with a genuinely COMPLEX transfer function.

    The shipped fixtures build ``complex_tf`` as ``10 ** (mag / 20)`` cast to
    complex — real and positive, so every phase is exactly 0.0 and a banked
    ``phase_deg`` of all zeros would pass a round-trip that never carried
    phase at all. This winds a ramp onto the same magnitudes so the assertion
    has something to be wrong about.
    """
    import numpy as np

    def _wind(response):
        size = np.asarray(response.freqs_hz).size
        return replace(
            response,
            complex_tf=np.abs(response.complex_tf) * np.exp(
                1j * np.linspace(-9.0, 9.0, size)
            ),
        )

    return replace(
        analysis,
        driver_responses=tuple(_wind(r) for r in analysis.driver_responses),
        summed_response=(
            _wind(analysis.summed_response)
            if analysis.summed_response is not None else None
        ),
    )


def _walk_to_banked_take(phase: str) -> tuple[dict, object]:
    """Walk a real session to ``phase`` and return its banked take + analysis.

    The analysis comes back beside the take because the assertion is an
    agreement between them: what the capture measured, and what the record
    says it measured.
    """
    seen: dict = {}

    def _probe(factory, key):
        def make(program, **kw):
            seen[key] = _phase_probe(factory(program, **kw))
            return seen[key]
        return make

    fakes = FakeSeams()
    fakes.measure = _probe(_measure_analysis, PHASE_MEASURE)
    # Every cloud position and VERIFY play the same verify-shaped summed
    # sweep, so one factory serves both and the last one analysed is the take
    # this walk is about.
    fakes.verify = _probe(_verify_analysis, "summed")
    retained: list = []
    if phase == PHASE_VERIFY:
        c = _conductor(
            fakes,
            seams=replace(fakes.seams(), bank_take=bank_into(retained)),
            index_phase_map=STAGE2_MAP,
            accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
            applied=True,
        )
        _run_phase(c, VERIFY_INDEX, 1)
    else:
        c = CrossoverV2Session(
            session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
            fc_hz=FC_HZ, driver_caps_dbfs=CAPS,
            session_volume_db=SESSION_VOLUME_DB,
            seams=replace(fakes.seams(), bank_take=bank_into(retained)),
            index_phase_map=CLOUD_MAP,
        )
        attempt = _walk(c, (1, 2), 1)
        if phase == PHASE_CLOUD_MEASURE:
            _walk(c, CLOUD_MEASURE_INDEXES, attempt)
    take = next(meta for meta in retained if meta["phase"] == phase)
    return take, seen[PHASE_MEASURE if phase == PHASE_MEASURE else "summed"]


@pytest.mark.parametrize(
    "phase,expected_roles",
    [
        (PHASE_MEASURE, ["woofer", "tweeter"]),
        (PHASE_VERIFY, ["summed"]),
        (PHASE_CLOUD_MEASURE, ["summed"]),
    ],
)
def test_every_banked_kind_carries_the_phase_its_analysis_measured(
    phase, expected_roles,
):
    """Ruling S3's other half — the acceptance row's *"``DriverResponse``
    banked"*, for the kinds that were not a walk pose.

    One kind banked phase and two did not: a pose carried ``curves``, the entry
    baseline carried magnitude alone, and a cloud seat and an unprompted-phase
    take carried no curve at all. Every one of those analyses computed the
    complex response in-process and dropped it, so a re-analysis re-derived
    phase from the WAVs and the forward model could never run from the bank.

    Asserted as a RECONSTRUCTION, not as key presence: the banked pair IS the
    transfer function, so the test rebuilds it and compares against the
    analysis's own values at the bins the record names.

    Parametrized by CARRY, deliberately: dropping one hop must red one row
    rather than the file. Two of the three hops are here — ``PHASE_MEASURE``
    and ``PHASE_VERIFY`` share ``_bank_phase_capture``'s single carry (one hop
    under two programs, so both rows go red together, which is what one hop
    breaking means), and ``PHASE_CLOUD_MEASURE`` is ``_retain_cloud_position``'s.
    The third, ``_retain_entry_baseline``'s, is pinned beside that phase's own
    retention tests in ``tests/test_crossover_v2_entry_baseline.py``.
    """
    import numpy as np

    take, analysis = _walk_to_banked_take(phase)
    sources = {
        r.role: r for r in (
            analysis.driver_responses
            or ((analysis.summed_response,) if analysis.summed_response else ())
        )
    }

    assert [curve["role"] for curve in take["curves"]] == expected_roles
    for curve in take["curves"]:
        source = sources[curve["role"]]
        rebuilt = 10.0 ** (np.asarray(curve["magnitude_db"]) / 20.0) * np.exp(
            1j * np.radians(np.asarray(curve["phase_deg"]))
        )
        # By the record's OWN account of which bins it sampled, not by
        # re-deriving the sampler here — that is the claim ``freqs_hz`` makes.
        at = {float(hz): i for i, hz in enumerate(source.freqs_hz)}
        sampled = [at[hz] for hz in curve["freqs_hz"]]
        assert np.allclose(rebuilt, np.asarray(source.complex_tf)[sampled])
        # Not vacuous: the fixture's wound phase really is non-zero.
        assert np.any(np.abs(np.asarray(curve["phase_deg"])) > 1.0)


def test_a_check_take_banks_no_curve_because_check_measures_none():
    """CHECK solves gains off pilots and computes no transfer function at all.

    The honest empty list, not an omission and not a claimed clean curve —
    ``_analyze_check`` returns a ``ProgramAnalysis`` with neither
    ``driver_responses`` nor ``summed_response``, so there is nothing for the
    carry to serialize and the record says exactly that.
    """
    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(fakes.seams(), bank_take=bank_into(retained)),
        index_phase_map=CLOUD_MAP,
    )
    _walk(c, (1, 2), 1)

    banked = {meta["phase"]: meta for meta in retained}
    assert banked[PHASE_CHECK]["curves"] == []
    assert banked[PHASE_MEASURE]["curves"] != []


def test_a_verify_take_banks_the_kind_its_own_round_can_derive():
    """VERIFY classifies; it does not bank an unresolved kind it could resolve.

    ``take_kind`` needs two named fingerprints: the graph this capture went
    through, and the round's pre-apply comparand. By VERIFY the session holds
    both — the entry baseline stage 1 took is what a post-apply re-measure is
    post-apply OF — so leaving the comparand unstated would bank ``""`` for a
    take whose kind the round already knows. CHECK and MEASURE genuinely
    cannot: CHECK is kindless by design, and MEASURE's comparand is minted
    after it banks.

    The two fingerprints must also DIFFER, which is what makes this a verify
    rather than a baseline — the same graph on both sides is the round that
    changed nothing.
    """
    retained: list = []
    fakes = FakeSeams()
    c = _conductor(
        fakes,
        seams=replace(
            fakes.seams(),
            bank_take=bank_into(retained),
            entry_graph_fingerprint=lambda: "fp-after-the-apply",
        ),
        index_phase_map=STAGE2_MAP,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    # The fixture's stage-2 baseline, whose graph is "fixture_entry_graph" —
    # named, and not the post-apply one above.
    assert c.measure_entry_baseline is not None

    _run_phase(c, VERIFY_INDEX, 1)

    banked = [m for m in retained if m["phase"] == PHASE_VERIFY]
    assert len(banked) == 1
    assert banked[0]["measure_kind"] == MEASURE_KIND_VERIFY


def test_an_unprompted_take_is_named_the_way_the_entry_baseline_named_its_own():
    """One take-id convention across the four phases that prompt no spot.

    The entry baseline hit this first — a retained capture with no table row —
    and answered it by minting the position id from the phase and the index, so
    that once ``take_id_for`` qualifies it by attempt the position id IS the
    take id. A second convention here would mean a reader had to know which
    phase wrote a take before it could parse its name.
    """
    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(fakes.seams(), bank_take=bank_into(retained)),
        index_phase_map=CLOUD_MAP,
    )
    _walk(c, (1, 2), 1)

    named = {meta["phase"]: meta for meta in retained}
    assert named[PHASE_CHECK]["take_id"] == f"{PHASE_CHECK}_01_a01"
    assert named[PHASE_MEASURE]["take_id"] == f"{PHASE_MEASURE}_02_a02"
    # The coincidence the entry baseline records: no prompted spot of its own,
    # so the position id and the take id are one string.
    for take in named.values():
        assert take["position_id"] == take["take_id"]


def _refuse_check(fakes):
    fakes.check = lambda program: _check_analysis(program, linearity=False)


def _refuse_measure(fakes):
    fakes.measure = lambda program: _measure_analysis(program, linearity=False)


def _refuse_verify(fakes):
    fakes.verify = lambda program: _verify_analysis(program, linearity=False)


@pytest.mark.parametrize(
    "phase", [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
)
def test_a_refused_capture_of_an_unprompted_phase_banks_nothing(phase):
    """Accepted-only, the rule every other retained kind already follows.

    A refused capture is evidence about the room or the phone, not about the
    speaker, and the journal is where that is recorded. Banking one would put a
    take in the bundle that the round never graded and offline analyze would
    have to learn to skip.

    Parametrized over all three because the rule is one rule and the arms are
    three call sites: pinning it on CHECK alone left deleting the guard from
    the MEASURE or the VERIFY arm invisible to the whole suite.
    """
    # Resolved here rather than in the parametrize: ``VERIFY_INDEX`` is
    # imported below this point in the module, so a decorator that named it
    # would not collect.
    refuse, warmup, index, stage_2 = {
        PHASE_CHECK: (_refuse_check, (), 1, False),
        PHASE_MEASURE: (_refuse_measure, (1,), 2, False),
        PHASE_VERIFY: (_refuse_verify, (), VERIFY_INDEX, True),
    }[phase]

    retained: list = []
    fakes = FakeSeams()
    refuse(fakes)
    kwargs = (
        {"index_phase_map": STAGE2_MAP,
         "accepted_phases": (PHASE_CHECK, PHASE_MEASURE), "applied": True}
        if stage_2 else {"index_phase_map": CLOUD_MAP}
    )
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(fakes.seams(), bank_take=bank_into(retained)),
        **kwargs,
    )
    for warm in warmup:
        _run_phase(c, warm, 1)

    verdict = _run_phase(c, index, 1)

    assert verdict["accepted"] is False
    assert [m for m in retained if m["phase"] == phase] == []
    # ...and the warm-up captures that WERE accepted still banked, so this is
    # reading a refusal rather than a seam that never fired.
    assert len(retained) == len(warmup)


def test_the_bank_seam_gets_every_accepted_position_with_its_prompt():
    """The forensic record the choreography owes: the prompt is the only durable
    statement of WHERE a curve was measured."""
    retained: list = []
    fakes = FakeSeams()
    seams = replace(
        fakes.seams(),
        bank_take=bank_into(retained, phase=PHASE_CLOUD_MEASURE),
    )
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=seams, index_phase_map=CLOUD_MAP,
    )
    attempt = _walk(c, (1, 2), 1)
    _walk(c, CLOUD_MEASURE_INDEXES, attempt)

    assert [meta["position_id"] for meta in retained] == [
        f"{PHASE_CLOUD_MEASURE}_{i:02d}" for i in CLOUD_MEASURE_INDEXES
    ]
    prompts = [meta["prompt"] for meta in retained]
    assert prompts == [p.text for p in CLOUD_POSITION_PROMPTS[: len(retained)]]
    assert sum(1 for meta in retained if meta["wide"]) >= 2
    # Each position's NAMED QUESTION rides its record, from the same table row
    # the prompt came from (attribution-stage plan §5 promotion-queue item 1).
    # The prompt string cannot be parsed back into a role, so the label is the
    # only way the attribution stage sees a labelled sample rather than an
    # anonymous member of an average — and it has to be the row's, not a guess.
    roles = [meta["role"] for meta in retained]
    assert roles == [p.role for p in CLOUD_POSITION_PROMPTS[: len(retained)]]
    # …and the shipped walk really does sample all three questions, which is
    # the point of labelling them at all: a walk that only ever produced one
    # role would be the same average with extra words.
    assert set(roles) == set(POSITION_ROLES)
    for meta in retained:
        assert meta["phase"] == PHASE_CLOUD_MEASURE
        assert meta["session_id"] == SESSION
        assert meta["captured_at"] > 0


def test_a_verify_pose_banks_its_angle_axis_and_distance_as_fields():
    """(T1-6) WHERE the microphone was, as numbers rather than as English.

    The defect this closes, measured against the banked artifacts of the
    2026-08 new-horn campaign: a ``cloud_verify`` position record carried no
    geometry field at all. Its only statement of place was the household
    ``prompt`` sentence — un-checkable, un-diffable, and the thing a reader
    interpreted as a mic being carried sideways when the rig had rotated.

    The owner's ruling names the three: angle, axis, distance. ``position_deg``
    deliberately spells the word ``lateral_pose_record`` already uses, so there
    is ONE vocabulary for "what bearing was this taken at" rather than two.
    """
    retained: list = []
    fakes = FakeSeams()
    fakes.apply_done = True
    c = _conductor(
        fakes,
        # The SAME ``fakes`` the conductor runs on, with one seam wrapped —
        # a second FakeSeams() here would silently drop ``apply_done``.
        seams=replace(
            fakes.seams(),
            bank_take=bank_into(retained, phase=PHASE_CLOUD_VERIFY),
        ),
        index_phase_map=STAGE2_MAP,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    _walk(c, (VERIFY_INDEX, *CLOUD_VERIFY_INDEXES), 1)

    assert [m["position_id"] for m in retained] == [
        f"{PHASE_CLOUD_VERIFY}_{i:02d}" for i in CLOUD_VERIFY_INDEXES
    ]
    # The shipped pose set, read back off the records rather than off the
    # table: the design axis first, then the four sides.
    assert [m["position_deg"] for m in retained] == [
        flow.position_angle_deg(p) for p in flow.CLOUD_VERIFY_POSE_PROMPTS
    ] == [0, -7, 7, -22, 22]
    assert {m["position_axis"] for m in retained} == {"horizontal"}
    assert {m["mark_distance_m"] for m in retained} == {flow.MARK_DISTANCE_M}
    # The prompt stays — it is the human instruction — but it is no longer the
    # only place the geometry lives.
    assert [m["prompt"] for m in retained] == [
        p.text for p in flow.CLOUD_VERIFY_POSE_PROMPTS
    ]
    assert all(m["prompt"] for m in retained)


def test_a_vertical_seat_states_its_elevation_and_still_banks_no_bearing():
    """A raised pose commands NO bearing, and 0 would read as the design axis.

    Where it WAS raised to is ``vertical_deg``, derived from the row's own
    ``offset_cm`` against the mark distance exactly as a lateral row's bearing
    is, and signed by the row's own ABOVE/BELOW word — so the two 40 cm rows
    stop being byte-identical records.

    ``position_angle_deg`` still refuses a vertical row outright, and that
    refusal is deliberately kept: it aims an external POSITIONER, and no
    positioner can raise the microphone. This derivation runs on the retention
    path instead, where a raise would fail a capture the household already
    gave, so it states the axis and leaves the angle ``None``.
    """
    vertical = [
        p for p in CLOUD_POSITION_PROMPTS if p.role == flow.POSITION_ROLE_XOVR
    ]
    geometries = [flow.position_geometry(p) for p in vertical]

    assert {g.axis for g in geometries} == {"vertical"}
    assert {g.degrees for g in geometries} == {None}
    assert {g.mark_distance_m for g in geometries} == {flow.MARK_DISTANCE_M}
    assert [g.vertical_deg for g in geometries] == [7, -7, 22, -22]
    for prompt in vertical:
        with pytest.raises(CrossoverV2FlowError):
            flow.position_angle_deg(prompt)


def test_the_compound_retake_rung_states_the_rise_it_asks_for():
    """The one shipped pose that moves BOTH ways states both, or it lies.

    Rung 2 asks for 75 cm sideways AND 30 cm up. Its two displacements differ,
    so a single ``offset_cm`` cannot carry them — and a record that defaulted
    its elevation to 0 would claim mark height for a microphone the household
    was told to raise, and would pair that take against a mark-height baseline.
    """
    rung_2 = flow.CloudPositionPrompt(
        flow.CLOUD_GEOMETRY_RETRY_PROMPTS[1],
        offset_cm=flow.GEOMETRY_RETRY_OFFSET_CM,
        role=flow.POSITION_ROLE_OFFAX,
        vertical_sign=1,
        vertical_offset_cm=flow.CLOUD_GEOMETRY_RETRY_RISE_CM[1],
    )
    geometry = flow.position_geometry(rung_2)

    assert flow.CLOUD_GEOMETRY_RETRY_RISE_CM[1] > 0
    assert geometry.vertical_deg == 17
    # Its lateral distance is the wider one and is NOT what the rise came from.
    assert flow.GEOMETRY_RETRY_OFFSET_CM != flow.CLOUD_GEOMETRY_RETRY_RISE_CM[1]
    # Rung 1 is at mark height and says so.
    assert flow.CLOUD_GEOMETRY_RETRY_RISE_CM[0] == 0.0


def test_a_raised_seat_joins_no_bearing_set_the_walk_already_had():
    """The mixed walk's horizontal aggregates do not notice the raised seats.

    The shipped cloud table is already mixed — seven lateral rows and four
    raised ones. Banking an elevation must not move what the horizontal-only
    consumers see, and the mechanism that guarantees it is ``position_deg``
    staying ``None`` on a raised seat: every pooled bearing set in the tree
    (``evidence_packet._angle_deg_block`` is the one a reader sees) is built by
    filtering for an ``int`` bearing, so a raised seat is excluded there and
    included, AS LABELLED, everywhere a seat is listed.
    """
    geometries = [flow.position_geometry(p) for p in CLOUD_POSITION_PROMPTS]
    bearings = [g.degrees for g in geometries if isinstance(g.degrees, int)]

    assert bearings == [-7, 7, -22, 22, -14, 14, -31]
    assert [
        flow.position_angle_deg(p) for p in CLOUD_POSITION_PROMPTS
        if p.role != flow.POSITION_ROLE_XOVR
    ] == bearings
    # Every lateral seat is at mark height, so the new field says nothing new
    # about any of them — which is why an old bundle missing it reads as 0.
    assert {
        g.vertical_deg for g in geometries if g.axis == "horizontal"
    } == {0}


def test_a_retake_records_the_prompt_it_was_actually_given(monkeypatch):
    """B3: the sidecar's prompt is the only durable statement of WHERE a curve
    was measured. A geometry retake follows a wider-spot rung, not the position
    table's entry — recording the table entry would name a spot the operator
    was explicitly told to abandon."""
    retained: list = []
    fakes = FakeSeams()
    c = CrossoverV2Session(
        session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=replace(
            fakes.seams(),
            bank_take=bank_into(retained),
        ),
        index_phase_map=CLOUD_MAP,
    )
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    _lock(monkeypatch)

    _run_phase(c, last, attempt)          # original take, then geometry-rejected
    attempt += 1
    _run_phase(c, last, attempt)          # first wider retake, rejected again
    attempt += 1
    monkeypatch.undo()
    _run_phase(c, last, attempt)          # second wider retake, accepted

    takes = [m for m in retained if m["index"] == last]
    assert len(takes) == 3
    # The original followed the table; both retakes followed their own rung, in
    # order, and are marked wide — the rungs ask for GEOMETRY_RETRY_OFFSET_CM,
    # past the wide class by design, and `wide` is computed from that distance
    # rather than hand-set (the body-part register this comment used to name
    # was withdrawn by #1805's 2026-07-28 ruling).
    assert takes[0]["prompt"] == CLOUD_POSITION_PROMPTS[
        len(CLOUD_MEASURE_INDEXES) - 1
    ].text
    assert takes[1]["prompt"] == CLOUD_GEOMETRY_RETRY_PROMPTS[0]
    assert takes[2]["prompt"] == CLOUD_GEOMETRY_RETRY_PROMPTS[1]
    assert takes[1]["wide"] is True and takes[2]["wide"] is True
    # Each take carries its own attempt — what disambiguates their artifacts.
    assert len({m["attempt"] for m in takes}) == 3
    # Only the LAST is in the cloud.
    surviving = c.group_position_takes(PHASE_CLOUD_MEASURE)
    assert [t["attempt"] for t in surviving if t["index"] == last] == [
        takes[2]["attempt"]
    ]


def test_group_combine_failure_degrades_to_an_unknown_verdict(monkeypatch):
    """A group's captures are already-accepted evidence; a combiner failure must
    not retroactively fail them."""
    def explode(_captures, **_kw):
        raise ValueError("malformed grid")

    # ``cloud_geometry_verdict`` imports the combiner lazily from its own
    # module, so patch it there rather than on the conductor's namespace.
    monkeypatch.setattr(
        "jasper.audio_measurement.spatial_combine.combine_positions", explode
    )
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    verdict = _run_phase(c, CLOUD_MEASURE_INDEXES[-1], attempt)
    assert verdict["accepted"] is True
    assert verdict["geometry"] == {
        "locked": False, "reason": "combine_failed",
        "n_positions": len(CLOUD_MEASURE_INDEXES),
    }


def test_cloud_session_phases_and_resume_within_the_same_session():
    """§5.6 unchanged: a cloud group interrupted mid-way resumes only within the
    SAME capture session. The session's own phase list rides the snapshot so a
    reader can tell a cloud session from a verify-only re-arm."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    # A STAGE-1 session's phases (work order D1): CHECK, MEASURE, the
    # pre-apply cloud — and deliberately no VERIFY, because the post-apply
    # sweep is stage 2's own session. This tuple is exactly what the wizard's
    # ``crossover_v2_phase`` reads to resolve the review interlude.
    assert c.session_phases == (
        PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE,
    )
    attempt = _walk(c, (1, 2), 1)
    _walk(c, CLOUD_MEASURE_INDEXES, attempt)
    snap = c.snapshot()
    assert PHASE_CLOUD_MEASURE in snap.accepted_phases
    assert snap.session_phases == c.session_phases

    resumed = CrossoverV2Session.hydrate(
        snap, session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(), index_phase_map=CLOUD_MAP,
    )
    assert PHASE_CLOUD_MEASURE in resumed.accepted_phases
    # Every phase this session runs is accepted; the journey continues in the
    # browser, not in another capture.
    assert resumed.current_phase == PHASE_DONE


def test_a_new_capture_session_invalidates_the_whole_cloud():
    """Mic position is unverifiable across sessions, so a fresh session restarts
    at CHECK — the cloud is evidence like any other phase, never an exception."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    _walk(c, CLOUD_MEASURE_INDEXES, attempt)

    fresh = CrossoverV2Session.hydrate(
        c.snapshot(), session_id="cap_a_different_session",
        source_preset=_preset(), roles_bands=_roles(), fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(), index_phase_map=CLOUD_MAP,
    )
    assert fresh.accepted_phases == frozenset()
    assert fresh.current_phase == PHASE_CHECK
    assert fresh.group_positions(PHASE_CLOUD_MEASURE) == ()
    assert fresh.group_geometry(PHASE_CLOUD_MEASURE) is None


def test_verify_only_rearm_session_never_waits_on_a_cloud_it_has_no_captures_for():
    """A conductor walks the phases ITS map addresses. The re-verify re-arm maps
    one index to VERIFY, so it must reach DONE rather than sitting pending on a
    position group that has no entry in its plan."""
    fakes = FakeSeams()
    c = _conductor(
        fakes, index_phase_map={1: PHASE_VERIFY},
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE),
        applied=True,
    )
    assert c.session_phases == (PHASE_VERIFY,)
    assert c.current_phase == PHASE_VERIFY
    _run_phase(c, 1, 1)
    assert c.current_phase == PHASE_DONE


def test_cloud_positions_play_the_summed_program_and_get_no_tracking_prior():
    """A cloud position is OFF the design axis by construction, so measured-vs-
    predicted divergence there is the spatial variation the cloud exists to
    sample — not a tracking error. Withholding ``predicted_sum`` means no
    tracking claim can be made from a capture that cannot support one."""
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    attempt = _walk(c, (1, 2), 1)
    _run_phase(c, CLOUD_MEASURE_INDEXES[0], attempt)

    played_phase, played_program = fakes.played[-1]
    assert played_phase == PHASE_CLOUD_MEASURE
    # The conductor's phase and the PROGRAM's phase are different vocabularies:
    # the program is the VERIFY-shaped summed sweep, which is exactly why
    # `analyze_program_capture` needed no new dispatch branch.
    assert played_program.phase == PHASE_VERIFY
    analyzed_phase, prog_phase, _result, priors, _geometry = fakes.analyzed[-1]
    # Issue #1855: the analyze seam must receive the FLOW's phase
    # (cloud_measure), not the program's own phase (verify) — a retention
    # seam that read ``program.phase`` instead mislabeled every cloud
    # position as "verify" because the program is byte-identical to VERIFY's.
    assert analyzed_phase == PHASE_CLOUD_MEASURE
    assert prog_phase == PHASE_VERIFY
    assert priors.predicted_sum is None
    assert priors.crossover_fc_hz == FC_HZ


def test_summed_sweep_phases_share_one_program_object():
    """The byte-safety invariant issue #1976's fix depends on, pinned
    directly (adversarial-gate SF2, PR #2028): ``program_for_phase`` must hand
    the phases that share a program the SAME object, not merely an equal one.
    Each object is composed once in ``__init__`` (see the "Programs" block) and
    returned unchanged — nothing upstream of this test caught a divergence
    here: mutating ``program_for_phase`` to hand cloud phases a
    freshly-composed (value-equal, object-distinct) program left the wider
    suite green, because everything else asserts on program CONTENT
    (segments, gains, ``.phase``), never object identity. If this ever goes
    false, `jasper/web/correction_crossover_v2.py`'s
    ``bind_production_play._play`` writes a ``summed_program.wav`` that is
    NOT what a genuine capture of that phase actually played.

    Since the 2026-08-18 prelude trim there are TWO such objects rather than
    one: the compared pair (VERIFY and the entry baseline, whose ``program_id``
    equality is #2291's before→after check and the delta probe's anchor check)
    and the position groups' unannounced twin. The identity requirement is the
    same for each; what it is not is a claim that all four are one program.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    assert c.program_for_phase(PHASE_CLOUD_MEASURE) is c.program_for_phase(
        PHASE_CLOUD_VERIFY
    )
    assert c.program_for_phase(PHASE_ENTRY_BASELINE) is c.program_for_phase(
        PHASE_VERIFY
    )
    assert c.program_for_phase(PHASE_CLOUD_VERIFY) is not c.program_for_phase(
        PHASE_VERIFY
    )


# --- capture plan (auto-advance policy, §5.2/§5.7) ---------------------------------


def test_capture_plan_entries_carry_auto_advance_policy():
    plan = build_v2_capture_plan(_roles(), FC_HZ)
    assert plan.schema_version == 2
    # RE-DERIVED for the two-stage split (work order D1/D2). The shipped
    # STAGE-1 plan is CHECK + MEASURE + N-1 prompted pre-apply positions:
    # 1 + 1 + 8 = 10 at the Full tier's DEFAULT_CLOUD_MEASURE_POSITIONS = 9.
    # It carries no VERIFY and no post-apply group — those are stage 2's plan,
    # pinned in test_the_stage_2_plan_walks_the_tiers_own_verify_shape.
    # ``cloud_capture_target()`` still names the WHOLE journey (10 + 6),
    # which is what the tier chooser promises. Stage 2's 6 is VERIFY's anchor
    # plus the five poses of ``CLOUD_VERIFY_POSE_PROMPTS`` (2026-08-24 ruling).
    assert plan.capture_target == 10
    assert cloud_capture_target() == 16
    kinds = [entry.kind_label for entry in plan.entries]
    assert kinds == (
        ["check", "measure"]
        + ["cloud_measure"] * (DEFAULT_CLOUD_MEASURE_POSITIONS - 1)
    )
    assert [entry.index for entry in plan.entries] == list(range(10))
    check, measure = plan.entries[0], plan.entries[1]
    # CHECK and MEASURE each take a tap. Every prompted cloud position needs
    # its own tap, because the operator has to physically move the mic
    # between them.
    assert check.screen["auto_advance"] == AUTO_ADVANCE_TAP
    # MEASURE used to auto-advance behind a 5 s cancelable countdown (same
    # spot, no movement needed). Issue #1823: it is also the session's longest
    # capture and the one that can be its loudest, and rolling into it unasked
    # read as the speaker taking a liberty — so it takes a tap, behind copy
    # that says what is coming. The countdown vocabulary is retained for a
    # future same-spot transition; it is simply unused by this entry, so the
    # countdown-only keys are gone with it.
    assert measure.screen["auto_advance"] == AUTO_ADVANCE_TAP
    assert "countdown_s" not in measure.screen
    assert "cancelable" not in measure.screen
    # HEDGED on purpose. #1825/#1829 solve each driver's MEASURE level to the
    # SNR the fit needs in its own band, so a quiet room gets a quiet MEASURE —
    # "louder" flat would be a promise the speaker no longer keeps.
    assert "can be the loudest" in measure.screen["body"]
    assert "louder —" not in measure.screen["body"]
    # The vocabulary itself survives the flip — the page still implements the
    # policy and a future same-spot transition can earn it back — but no
    # SHIPPED entry uses it today. Pinned so "unused, delete it" and "silently
    # reinstated on MEASURE" are both visible changes.
    assert AUTO_ADVANCE_COUNTDOWN_S > 0
    assert all(
        entry.screen.get("auto_advance") != AUTO_ADVANCE_COUNTDOWN
        for entry in plan.entries
    )
    for entry in plan.entries:
        if entry.kind_label.startswith("cloud_"):
            assert entry.screen["auto_advance"] == AUTO_ADVANCE_TAP
            # The redesign's grammar (§2.1): the INSTRUCTION is the title, the
            # supporting clause is the body and may legitimately be empty.
            assert entry.screen["title"]
            assert "body" in entry.screen
    # No entry of a STAGE-1 plan arms on an apply — there is no apply in this
    # session to arm on (work order D1/D10).
    assert all(
        entry.screen.get("auto_advance") != AUTO_ADVANCE_ON_APPLY
        for entry in plan.entries
    )
    # …and the END screen is stage 2's, not stage 1's: nothing here may claim
    # the speaker is tuned. (The generic page fallback a stage-1 plan therefore
    # falls back to is PR-T4's; see the work order's D7 list.)
    assert all("done_title" not in entry.screen for entry in plan.entries)
    # Durations are per-entry (heterogeneous) and positive.
    assert all(entry.duration_ms > 0 for entry in plan.entries)
    assert len({entry.duration_ms for entry in plan.entries}) > 1


def test_capture_plan_index_phase_map_matches_the_emitted_entries():
    """The prompt an entry carries and the phase the conductor runs for that
    index come from the same builder — a drift here would prompt "move left"
    while the conductor analysed a VERIFY."""
    plan = build_v2_capture_plan(_roles(), FC_HZ)
    index_phase = build_v2_cloud_index_phase_map()
    assert len(index_phase) == plan.capture_target
    kind_for_phase = {
        PHASE_CHECK: "check",
        PHASE_MEASURE: "measure",
        PHASE_CLOUD_MEASURE: "cloud_measure",
        PHASE_VERIFY: "verify",
        PHASE_CLOUD_VERIFY: "cloud_verify",
    }
    for entry in plan.entries:
        # Entry indexes are 0-based; the capture's own index space is 1-based.
        assert entry.kind_label == kind_for_phase[index_phase[entry.index + 1]]


