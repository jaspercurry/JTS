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
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_LOCATE_FAILED,
    REASON_REGISTRY,
    locate_failed_diagnosis,
)
from jasper.active_speaker.crossover_v2_flow import (
    AUTO_ADVANCE_COUNTDOWN,
    AUTO_ADVANCE_COUNTDOWN_S,
    AUTO_ADVANCE_TAP,
    CLOUD_POSITION_PROMPTS,
    CrossoverV2Session,
    CrossoverV2FlowError,
    build_v2_capture_plan,
    build_v2_cloud_index_phase_map,
)
from jasper.audio_measurement import gating
from jasper.audio_measurement import snr_policy
from jasper.audio_measurement.program_analysis.model import DRIVER_SNR_ALIGNMENT_KEY
from jasper.audio_measurement.quality_model import DRIVER
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginRefused
from tests.crossover_v2_fixtures import (
    CAPS,
    bank_into,
    CLOUD_MAP,
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
    _check_analysis_with_solves,
    _conductor,
    _measure_analysis,
    _preset,
    _roles,
    _run_phase,
    _stage2_after_measure,
    _verify_analysis,
    _walk,
)


@pytest.mark.parametrize("case", ["improved", "still_weak", "resume", "driver_cap", "partial_cap", "flat_ceiling", "clipped"])
def test_measured_alignment_snr_prices_retries_within_the_admitted_gain(case):
    fakes = FakeSeams()

    def check(program):
        result = _check_analysis_with_solves(program)
        if case == "flat_ceiling":
            plan = result.gain_plan
            result = replace(result, gain_plan=replace(plan, role_solves={
                role: replace(solve, flat_target_gain_db=solve.gain_db)
                for role, solve in plan.role_solves.items()
            }))
        return result

    def measure(program, tweeter_snr=33.7):
        result = _measure_analysis(program)
        responses = []
        for response in result.driver_responses:
            snr_db = tweeter_snr if response.role == "tweeter" else 44.2
            block = snr_policy.band_snr_verdicts(
                decision_class=snr_policy.DECISION_CLASS_ALIGNMENT,
                capture_bands=[{"band_id": "mid", "band_hz": [1000, 4000], "level_dbfs": -70 + snr_db}],
                noise_bands=[{"band_id": "mid", "level_dbfs": -70}],
                noise_floor_dbfs_scalar=None, relevant_hz=(1600, 4000), model=DRIVER,
            )
            responses.append(replace(response, snr={DRIVER_SNR_ALIGNMENT_KEY: block}))
        return replace(result, driver_responses=tuple(responses), mic_meter_status="usable")

    fakes.check, fakes.measure = check, measure
    caps = {"woofer": 0.0, "tweeter": {"driver_cap": -65.0, "partial_cap": -48.0}.get(case, 0.0)}
    banked = []

    def bank(result, record):
        if record["phase"] == PHASE_MEASURE:
            banked.append((record, c.program_for_phase(PHASE_MEASURE).program_id))
        return record["take_id"]

    seams = replace(fakes.seams(), bank_take=bank)
    c = _conductor(fakes, driver_caps_dbfs=caps, seams=seams)
    assert _run_phase(c, 1, 1)["accepted"]
    original = c.program_for_phase(PHASE_MEASURE)
    if case == "clipped":
        fakes.measure = lambda program: _measure_analysis(program, clipped=True)
        clipped = _run_phase(c, 2, 2)
        assert clipped["code"] == refusal_copy.REASON_CLIPPED
        fakes.measure = measure
        quieter = c.program_for_phase(PHASE_MEASURE)
        assert quieter.segment("sweep_t").gain_db < original.segment("sweep_t").gain_db
        assert not _run_phase(c, 2, 3)["capabilities"]["delay_estimate"]
        assert c.program_for_phase(PHASE_MEASURE).program_id == quieter.program_id
        return

    first = _run_phase(c, 2, 2)
    assert first["evidence"]["mic_meter_status"] == "usable"
    if case in {"driver_cap", "flat_ceiling"}:
        assert not first["accepted"] and first["next"] == "fix_and_retake"
        assert not first["capabilities"]["delay_estimate"]
        assert c.program_for_phase(PHASE_MEASURE).program_id == original.program_id
        return

    assert not first["accepted"] and first["auto_retry"] and first["kept_measurement"]
    assert first["code"] == refusal_copy.REASON_MEASURE_GAIN_ADJUSTED
    assert first["gain_adjustment"]["source_program_id"] == original.program_id
    assert banked[0][1] == original.program_id and len(banked[0][0]["curves"]) == 2
    assert not fakes.published_candidates and PHASE_MEASURE not in c.accepted_phases
    retry = c.program_for_phase(PHASE_MEASURE)
    assert retry.program_id != original.program_id
    assert retry.segment("sweep_w").gain_db == original.segment("sweep_w").gain_db
    expected_gain = -28.01 if case == "partial_cap" else -23.7
    assert retry.segment("sweep_t").gain_db == pytest.approx(expected_gain)
    assert first["gain_adjustment"]["next_program_id"] == retry.program_id
    snapshot = c.snapshot()
    assert snapshot.gain_plan_db["tweeter"] == pytest.approx(expected_gain)
    if case == "resume":
        c = CrossoverV2Session.hydrate(
            snapshot, session_id=SESSION, source_preset=_preset(), roles_bands=_roles(),
            fc_hz=FC_HZ, driver_caps_dbfs=caps, session_volume_db=SESSION_VOLUME_DB,
            seams=seams, index_phase_map=CLOUD_MAP,
        )
        assert c.program_for_phase(PHASE_MEASURE).program_id == retry.program_id
    if case in {"improved", "resume"}:
        fakes.measure = lambda program: measure(program, 41.0)
    second = _run_phase(c, 2, 3)
    assert second["accepted"] is (case in {"improved", "resume"})
    assert second["next"] == {"still_weak": "retake_louder", "partial_cap": "fix_and_retake"}.get(case, "accept")
    assert second["evidence"]["mic_meter_status"] == "usable"
    if case != "resume":
        assert second["attempts"]["by_speaker"] == 1
        assert second["attempts"]["by_household"] == 0


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
    c = _stage2_after_measure(fakes)
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


@pytest.mark.parametrize("fault", ["glitch", "clip"])
def test_speaker_retries_have_a_total_bound_and_keep_the_operator_budget(fault):
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(program, glitch=fault == "glitch", clipped=fault == "clip")
    bound = flow._admission.MAX_AUTOMATIC_RETAKES_PER_POSITION
    for retry in range(bound + 1):
        result = _run_phase(c, 2, retry + 2)
        assert result["attempts"]["by_household"] == 0
        assert result["attempts"]["left"] == min(flow.MAX_EXTRA_ATTEMPTS_PER_POSITION, bound - retry)
        assert result["attempts"]["by_speaker"] == retry
        assert result.get("terminal", False) is (retry == bound)
    assert result["next"] == "stop" and not result["auto_retry"]
    with pytest.raises(CaptureBeginRefused):
        c.authorize_begin(2, bound + 3)


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
        _walk(c, (1, 2), 1)
    take = next(meta for meta in retained if meta["phase"] == phase)
    return take, seen[PHASE_MEASURE if phase == PHASE_MEASURE else "summed"]


@pytest.mark.parametrize(
    "phase,expected_roles",
    [
        (PHASE_MEASURE, ["woofer", "tweeter"]),
        (PHASE_VERIFY, ["summed"]),
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
    breaking means).
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


def test_verify_only_rearm_session_never_waits_on_a_cloud_it_has_no_captures_for():
    """A conductor walks the phases ITS map addresses. The re-verify re-arm maps
    one index to VERIFY, so it must reach DONE rather than sitting pending on a
    position group that has no entry in its plan."""
    fakes = FakeSeams()
    c = _conductor(
        fakes, index_phase_map={1: PHASE_VERIFY},
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    assert c.session_phases == (PHASE_VERIFY,)
    assert c.current_phase == PHASE_VERIFY
    _run_phase(c, 1, 1)
    assert c.current_phase == PHASE_DONE


# --- capture plan (auto-advance policy, §5.2/§5.7) ---------------------------------


def test_capture_plan_entries_carry_auto_advance_policy():
    plan = build_v2_capture_plan(_roles(), FC_HZ)
    assert plan.schema_version == 2
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
        PHASE_VERIFY: "verify",
        PHASE_CLOUD_VERIFY: "cloud_verify",
    }
    for entry in plan.entries:
        # Entry indexes are 0-based; the capture's own index space is 1-based.
        assert entry.kind_label == kind_for_phase[index_phase[entry.index + 1]]
