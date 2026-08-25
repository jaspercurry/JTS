# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A drifted fader must never read as a measurement (#2925).

The subject is the 2026-08-23/24 overnight campaign. Every MEASURE-phase
stimulus played at the household volume (−21.212124) while the session plan
declared the measurement volume (−12.5), all night, every round.
``play_program`` brackets every measure-phase stimulus in a graph load/restore
pair, and the fader was not surviving it.

**The mechanism, corrected (#2929).** #2925 recorded it as "a CamillaDSP
``SetConfig`` replace re-applies the incoming config's STORED volume". It does
not: CamillaDSP v4.1.3 has no config field that stores a volume, rejects
unknown ``devices`` keys outright, and keeps the fader across a reload. The
writer was JTS's own graph-swap duck — ``_duck_release_target_db`` releases to
``min(canonical, released)``, where ``canonical`` is the household target
``percent_to_db(listening_level)``, which is below any measurement volume and
therefore won that ``min`` on every capture. This file's own ``HOUSEHOLD_DB``
is the proof: −21.212124 is ``percent_to_db(58)`` to within 3 µdB of a float
readback, and the other two dB values #2925 attributed to configs are
``percent_to_db(81)`` and ``percent_to_db(64)``. The swap now takes the level
the session plan owns as its release reference, so the fader lands on the
declared volume by construction and the hold below is a TRIPWIRE — in a
healthy routed session it reads in tolerance and writes nothing.

Two halves ship together and are pinned together here:

* **T1-1** — the play path re-asserts and PROVES the declared measurement
  volume per stimulus, and refuses the capture when it cannot. The safety
  framing is not measurement tidiness: ``readmit_program_from_wav`` admitted
  every program against the DECLARED volume, so the excitation-safety ledger
  was wrong for every capture that night. It was wrong QUIET; the identical
  seam reversed is LOUD.
* **T1-2** — the retained record already carried the answer. ``main_volume_db``
  and ``session_volume_db`` sat two fields apart disagreeing by 8.712 dB from
  the first walk, and nothing compared them.

``test_the_two_capture_paths_share_one_fader_hold`` is this file's pin: the
defect lived in the split between the routed MEASURE path and the summed VERIFY
path, so the discipline is one seam and a per-phase copy would rebuild it.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from jasper.active_speaker.capture_provenance import (
    GRAPH_KIND_APPLIED,
    CaptureProvenance,
    volume_fields_agree,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_CHECK, PHASE_VERIFY
from jasper.active_speaker.crossover_v2_flow import (
    REASON_MEASUREMENT_VOLUME_DRIFT,
    REASON_REGISTRY,
)
from jasper.active_speaker.volume_latch import (
    READBACK_TOLERANCE_DB,
    MeasurementFaderDrift,
    fader_matches,
    hold_fader_at,
)
from jasper.audio_measurement.program import (
    FrequencyBand,
    RoleBand,
    build_check_program,
)
from jasper.web import correction_crossover_v2 as v2host

LATCH_LOGGER = "jasper.active_speaker.volume_latch"

#: The campaign's own numbers, so a reader can line the fixtures up with the
#: banked evidence: the plan declared this, the fader sat at HOUSEHOLD_DB, and
#: the gap is the 8.712 dB a per-octave fit of the banked curves recovered.
DECLARED_DB = -12.5
HOUSEHOLD_DB = -21.212124


# --------------------------------------------------------------------------- #
# the primitive: one agreement test, one hold
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("observed", "agrees"),
    [
        (DECLARED_DB, True),
        # The two live reads in the campaign's own evidence differ at µdB
        # level between captures; the tolerance has to pass those and nothing
        # a household could hear.
        (DECLARED_DB + 1e-6, True),
        (DECLARED_DB - 0.8 * READBACK_TOLERANCE_DB, True),
        (DECLARED_DB - 2 * READBACK_TOLERANCE_DB, False),
        (HOUSEHOLD_DB, False),
        (None, False),
        (True, False),  # a bool is not a fader reading
        ("-12.5", False),
        (float("nan"), False),
        (float("-inf"), False),
    ],
)
def test_fader_matches_is_the_one_agreement_test(observed, agrees):
    """Fail-closed in every direction: only a real, finite, in-tolerance number
    agrees. "Could not read" must never render as "matches"."""
    assert fader_matches(observed, DECLARED_DB) is agrees


def _fader(*, value: Any, set_ok: bool = True, sticks: bool = True) -> Any:
    """A fader stand-in: one live value, plus a setter that may not stick.

    ``sticks=False`` is the case worth naming — the setter reports success and
    the fader does not move, which is what "something else owns the volume"
    looks like from here.
    """
    state: dict[str, Any] = {"value": value, "writes": [], "reads": 0}

    async def get_db() -> Any:
        state["reads"] += 1
        return state["value"]

    async def set_db(db: float) -> bool:
        state["writes"].append(float(db))
        if set_ok and sticks:
            state["value"] = float(db)
        return set_ok

    return SimpleNamespace(get=get_db, set=set_db, state=state)


def test_an_undrifted_fader_costs_one_read_and_no_write():
    """The happy path is the common path: read, agree, done. Nothing is
    written, so the hold cannot itself become a source of volume churn."""
    fader = _fader(value=DECLARED_DB)
    proven = asyncio.run(
        hold_fader_at(DECLARED_DB, fader.set, fader.get, context="check")
    )
    assert proven == DECLARED_DB
    assert fader.state["writes"] == []
    assert fader.state["reads"] == 1


def test_a_drifted_fader_is_repaired_disclosed_and_re_proven(caplog):
    """The T1-1 fix. A fader left off the declared level — by the swap duck's
    release before #2929, by anything at all after it — is repaired before any
    audio, and DISCLOSED, because a silently repaired fader is how a whole
    campaign of stimuli can play at the wrong level without one line of
    evidence."""
    fader = _fader(value=HOUSEHOLD_DB)
    with caplog.at_level(logging.WARNING, logger=LATCH_LOGGER):
        proven = asyncio.run(
            hold_fader_at(
                DECLARED_DB, fader.set, fader.get, context="capture:check"
            )
        )
    assert proven == DECLARED_DB
    assert fader.state["writes"] == [DECLARED_DB]
    assert "result=repairing" in caplog.text
    assert "result=repaired" in caplog.text
    # Both values named, not just the delta — a forensic reader needs to know
    # WHICH of the two is the surprise.
    assert f"observed_db={HOUSEHOLD_DB:.6f}" in caplog.text
    assert f"expected_db={DECLARED_DB:.6f}" in caplog.text


@pytest.mark.parametrize(
    ("kwargs", "why", "observed"),
    [
        # The setter reports success and the fader still is not there —
        # something else is holding it.
        ({"value": HOUSEHOLD_DB, "sticks": False}, "held elsewhere", HOUSEHOLD_DB),
        ({"value": HOUSEHOLD_DB, "set_ok": False}, "the setter refused", HOUSEHOLD_DB),
        # Unreadable is equally unprovable, and an unprovable level is not a
        # measurement. (A fader that reads again AFTER the set is a different
        # case and is deliberately allowed through — see the test below.)
        ({"value": None, "sticks": False}, "unreadable", None),
    ],
)
def test_an_unrepairable_fader_refuses_rather_than_returning(kwargs, why, observed):
    """...and reports the observation it ACTUALLY made in each case.

    ``observed_db`` is the documented discriminator — refusal_copy and the
    HANDOFF row both say an empty one means "could not read".
    It only discriminates if the proving re-read is unconditional: while it
    short-circuited on a failed set-and-confirm, two of these three cases
    reported "unreadable" for a fader that had read fine at −21.212124 and
    simply would not move. That is the ``locate_failed`` #2085 class — copy
    stating an observation JTS never made — inside the change that exists to
    close it.
    """
    fader = _fader(**kwargs)
    with pytest.raises(MeasurementFaderDrift) as raised:
        asyncio.run(
            hold_fader_at(
                DECLARED_DB, fader.set, fader.get, context="capture:check"
            )
        )
    assert raised.value.expected_db == DECLARED_DB
    assert raised.value.context == "capture:check"
    assert raised.value.observed_db == observed, why
    # The message names both sides, never a bare slug.
    assert f"{DECLARED_DB:.6f}" in str(raised.value)
    if observed is None:
        assert "unreadable" in str(raised.value)
    else:
        assert f"{observed:.6f}" in str(raised.value)
        assert "unreadable" not in str(raised.value)


def _refusal_fields(caplog, fader) -> dict[str, str]:
    """Drive one refusal and parse the journal line a support read would have."""
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger=LATCH_LOGGER):
        with pytest.raises(MeasurementFaderDrift):
            asyncio.run(hold_fader_at(DECLARED_DB, fader.set, fader.get))
    line = next(
        rec.getMessage()
        for rec in caplog.records
        if "result=refused" in rec.getMessage()
    )
    return dict(token.split("=", 1) for token in line.split() if "=" in token)


def test_an_empty_observed_db_means_unreadable_and_nothing_else(caplog):
    """The one clean discriminator on the refusal line, asserted as a
    biconditional rather than in one direction: a fader that could not be read
    reports empty, and a fader that read and would not move reports the value
    it read. Before the proving re-read became unconditional, two of these
    three said "unreadable" about a fader they had read at −21.212124."""
    held = _refusal_fields(caplog, _fader(value=HOUSEHOLD_DB, sticks=False))
    refused = _refusal_fields(caplog, _fader(value=HOUSEHOLD_DB, set_ok=False))
    unreadable = _refusal_fields(caplog, _fader(value=None, sticks=False))

    assert unreadable["observed_db"] == '""'
    assert held["observed_db"] == f"{HOUSEHOLD_DB:.6f}"
    assert refused["observed_db"] == f"{HOUSEHOLD_DB:.6f}"


def test_set_confirmed_does_not_claim_to_separate_the_two_stuck_cases(caplog):
    """A guard on the PROSE, because the first draft of it was wrong.

    ``set_confirmed`` is the set-AND-confirm's verdict, not the setter's, so a
    setter that refused and a setter that reported success onto a fader that
    did not move both land ``false``. The docstring said it told them apart;
    this is what proved otherwise, and it stays so the claim cannot come back.
    """
    held = _refusal_fields(caplog, _fader(value=HOUSEHOLD_DB, sticks=False))
    refused = _refusal_fields(caplog, _fader(value=HOUSEHOLD_DB, set_ok=False))
    assert held["set_confirmed"] == refused["set_confirmed"] == "false"


def test_set_confirmed_true_on_a_refusal_means_the_fader_moved_again(caplog):
    """What the field IS for: the repair was confirmed at the target and the
    fader moved again before the proving read — something is contending for it
    in real time, which is neither of the two cases above."""
    state = {"value": HOUSEHOLD_DB, "reads": 0}

    async def get_db() -> float:
        state["reads"] += 1
        # Reads 1 (initial) and 2 (set-and-confirm's own readback) behave
        # normally; the third — the independent proving read — finds it moved.
        if state["reads"] == 3:
            return HOUSEHOLD_DB
        return state["value"]

    async def set_db(db: float) -> bool:
        state["value"] = float(db)
        return True

    fields = _refusal_fields(caplog, SimpleNamespace(set=set_db, get=get_db))
    assert fields["set_confirmed"] == "true"
    assert fields["observed_db"] == f"{HOUSEHOLD_DB:.6f}"


def test_a_transiently_unreadable_fader_is_proven_by_the_repair_not_refused():
    """Disclose, never force. A read that fails once and answers after the set
    has still PROVEN the level, so the capture proceeds — the refusal is for a
    level that cannot be established, not for one round-trip that missed."""
    fader = _fader(value=None)
    proven = asyncio.run(
        hold_fader_at(DECLARED_DB, fader.set, fader.get)
    )
    assert proven == DECLARED_DB
    assert fader.state["writes"] == [DECLARED_DB]


def test_a_wedged_fader_read_refuses_instead_of_raising_its_own_error():
    """``CamillaUnavailable`` reaches this seam as ``RuntimeError`` (the
    caller's ``_session_volume_io`` wraps it). It must land as the refusal, not
    as an unclassified error the catch-all arm calls ``internal_error``."""

    async def boom() -> float:
        raise RuntimeError("CamillaDSP is unavailable")

    async def set_db(db: float) -> bool:
        return True

    with pytest.raises(MeasurementFaderDrift):
        asyncio.run(hold_fader_at(DECLARED_DB, set_db, boom))


# --------------------------------------------------------------------------- #
# the refusal reaches the household as its own honest code
# --------------------------------------------------------------------------- #


def test_the_drift_refusal_is_classified_as_its_own_reason():
    """Never ``program_unplayable``: that code's copy says JTS "could not play
    the measurement signal within the speaker's safe limits", which is the
    opposite of what happened — the program was admissible and the SPEAKER's
    level was not the one it was admitted against (the #1820 rule that a
    refusal needing a different action needs a different code)."""
    from jasper.active_speaker.crossover_v2_flow import REASON_PROGRAM_UNPLAYABLE

    classified = v2host.classify_program_failure(
        MeasurementFaderDrift(
            expected_db=DECLARED_DB, observed_db=HOUSEHOLD_DB, context="capture:check"
        )
    )
    assert classified == (REASON_MEASUREMENT_VOLUME_DRIFT, ())
    assert REASON_MEASUREMENT_VOLUME_DRIFT != REASON_PROGRAM_UNPLAYABLE


def test_the_drift_reason_is_terminal_and_not_retried_around():
    """Campaign guardrail 2, verbatim: "a safety refusal is not something to
    retry around." The re-assert has already been tried and could not be
    confirmed, so another attempt at the same capture only re-runs it against
    whatever is holding the fader."""
    from jasper.active_speaker.crossover_v2_flow import TEMPLATE_HARD_STOP

    spec = REASON_REGISTRY[REASON_MEASUREMENT_VOLUME_DRIFT]
    assert spec.template == TEMPLATE_HARD_STOP
    assert spec.retry_budget == 0
    assert REASON_MEASUREMENT_VOLUME_DRIFT not in spec.message


def test_the_drift_copy_names_the_observation_and_never_a_cause():
    """Two conditions reach this code — a fader that will not hold, and one
    that cannot be read at all — so copy asserting that something CHANGED the
    volume would be false for the second and would send that household after a
    cause nobody measured. `locate_failed`'s #2085 lesson, applied before it
    could bite: the sentence says what JTS could not confirm."""
    message = REASON_REGISTRY[REASON_MEASUREMENT_VOLUME_DRIFT].message
    assert "could not confirm" in message
    for cause in ("changed the volume", "moved while", "something else"):
        assert cause not in message, cause


# --------------------------------------------------------------------------- #
# T1-2: the record was printing the defect from round one
# --------------------------------------------------------------------------- #


def _provenance(main: Any, session: Any) -> CaptureProvenance:
    return CaptureProvenance(
        graph_kind=GRAPH_KIND_APPLIED, main_volume_db=main, session_volume_db=session,
    )


def test_the_campaign_s_own_two_fields_are_caught():
    """The exact pair every measure capture carried on 2026-08-24, two fields
    apart, for a whole night: −21.212124 live against −12.5 declared."""
    assert volume_fields_agree(_provenance(HOUSEHOLD_DB, DECLARED_DB)) is False
    assert volume_fields_agree(_provenance(DECLARED_DB, DECLARED_DB)) is True


def test_a_record_that_declares_a_volume_it_could_not_read_disagrees():
    """A ``null`` fader beside a declared volume is a record claiming a level it
    cannot support — a contradiction, not an absence."""
    assert volume_fields_agree(_provenance(None, DECLARED_DB)) is False


def test_no_declared_volume_is_not_a_disagreement():
    """With no measurement volume open the record makes no claim these two
    fields could contradict, so it is not flagged."""
    assert volume_fields_agree(_provenance(HOUSEHOLD_DB, None)) is True


def test_the_tripwire_and_the_refusal_share_one_tolerance():
    """One vocabulary per question. The record's disclosure and the play path's
    refusal must never disagree about what "agree" means, so both read the same
    constant — the one ``SessionVolumePlan.open`` confirmed the volume under."""
    just_inside = DECLARED_DB + 0.8 * READBACK_TOLERANCE_DB
    just_outside = DECLARED_DB + 2 * READBACK_TOLERANCE_DB
    assert volume_fields_agree(_provenance(just_inside, DECLARED_DB)) is True
    assert volume_fields_agree(_provenance(just_outside, DECLARED_DB)) is False

    inside = _fader(value=just_inside)
    assert asyncio.run(
        hold_fader_at(DECLARED_DB, inside.set, inside.get)
    ) == just_inside
    assert inside.state["writes"] == []

    outside = _fader(value=just_outside, sticks=False)
    with pytest.raises(MeasurementFaderDrift):
        asyncio.run(hold_fader_at(DECLARED_DB, outside.set, outside.get))


# --------------------------------------------------------------------------- #
# the plan owns the stateful half — and serializes it against its own drains
# --------------------------------------------------------------------------- #


def _open_plan(tmp_path, *, at_db: float = DECLARED_DB):
    """A real, opened ``SessionVolumePlan`` plus the fader it opened onto."""
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeOpenResult,
        SessionVolumePlan,
    )

    fader = _fader(value=HOUSEHOLD_DB)
    plan = SessionVolumePlan(state_path=tmp_path / "session_volume.json")
    opened = asyncio.run(plan.open(at_db, fader.set, fader.get))
    assert opened is SessionVolumeOpenResult.OPENED
    fader.state["writes"].clear()
    return plan, fader


def test_the_plan_holds_its_own_declared_volume(tmp_path):
    """The stateful seam: the plan answers with the volume IT opened, so the
    capture path never has to carry a second copy of that number."""
    plan, fader = _open_plan(tmp_path)
    # Where the swap duck's release used to leave the fader: its canonical
    # reference is the household level (#2929).
    fader.state["value"] = HOUSEHOLD_DB
    assert asyncio.run(
        plan.hold_measurement_volume(fader.set, fader.get, context="capture:check")
    ) == DECLARED_DB
    assert fader.state["writes"] == [DECLARED_DB]


def test_a_drained_plan_still_reports_its_volume_and_must_not_hold_it(tmp_path):
    """THE SAFETY GUARD ON THE FIX ITSELF.

    ``_mark_unresolved`` copies ``measurement_volume_db`` forward, so a plan
    that FAILED to restore still reports the declared volume. Reading that
    field unguarded would let a hold raise the fader back up on a speaker whose
    session had just given up — so the readiness check, not the field, decides.
    """
    plan, fader = _open_plan(tmp_path)
    # A restore that cannot be confirmed: the setter reports success and
    # nothing moves, at a level that is neither the exact original nor the
    # emergency floor, so neither candidate confirms and the plan latches
    # unresolved.
    stuck = _fader(value=-5.0, sticks=False)
    asyncio.run(plan.close(stuck.set, stuck.get))
    assert plan.needs_recovery
    assert plan.measurement_volume_db == DECLARED_DB  # the field still says it

    after = _fader(value=HOUSEHOLD_DB)
    assert asyncio.run(
        plan.hold_measurement_volume(after.set, after.get)
    ) is None
    assert after.state["writes"] == []


def test_a_restored_plan_holds_nothing(tmp_path):
    """After a clean close there is no volume to hold, so the household's own
    level is left exactly where the restore put it."""
    plan, fader = _open_plan(tmp_path)
    asyncio.run(plan.close(fader.set, fader.get))
    fader.state["writes"].clear()
    assert asyncio.run(plan.hold_measurement_volume(fader.set, fader.get)) is None
    assert fader.state["writes"] == []


def test_a_stale_session_past_its_ceiling_holds_nothing(tmp_path):
    """The wall-clock ceiling exists so a walked-away session cannot pin the
    speaker at measurement volume. A hold that ignored staleness would pin it
    right back — once per capture, indefinitely."""
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeOpenResult,
        SessionVolumePlan,
    )

    now = [1000.0]
    fader = _fader(value=HOUSEHOLD_DB)
    plan = SessionVolumePlan(
        state_path=tmp_path / "session_volume.json",
        wall_clock_ceiling_s=10.0,
        clock=lambda: now[0],
    )
    assert asyncio.run(
        plan.open(DECLARED_DB, fader.set, fader.get)
    ) is SessionVolumeOpenResult.OPENED
    fader.state["value"] = HOUSEHOLD_DB
    fader.state["writes"].clear()

    now[0] = 1000.0 + 11.0  # past the ceiling
    assert plan.stale_active()
    assert asyncio.run(plan.hold_measurement_volume(fader.set, fader.get)) is None
    assert fader.state["writes"] == []


def test_a_drain_that_confirmed_but_could_not_persist_still_holds_nothing(
    tmp_path, caplog
):
    """The third case in "either precedes the drain or sees the drained state".

    Serializing against the drains is necessary and was not sufficient: a drain
    can fail PARTWAY — the fader confirmed on the hardware, then an OSError
    writing the resolved marker (read-only or full ``/var/lib/jasper``). While
    ``_clear_resolved`` persisted BEFORE clearing, that raised out with the
    plan still ``active`` and ``measurement_volume_db`` intact, so
    ``assert_ready`` passed and the next hold commanded −12.5 onto a speaker
    sitting at the emergency floor: **+47.5 dB**. Clearing in-memory first is
    what closes it, and the CRITICAL line is what keeps the lost marker loud.
    """
    from jasper.active_speaker import session_volume_plan as plan_mod

    plan, fader = _open_plan(tmp_path)

    # A restore that confirms the EMERGENCY floor: the exact original is
    # unreachable (the fader sits somewhere else and will not take it), the
    # floor is not.
    floor = plan_mod.EMERGENCY_MEASUREMENT_VOLUME_DB
    drain = _fader(value=-5.0)

    async def set_only_the_floor(db: float) -> bool:
        drain.state["writes"].append(float(db))
        if float(db) == float(floor):
            drain.state["value"] = float(db)
        return True

    def boom(payload):
        raise OSError("read-only file system")

    with caplog.at_level(logging.CRITICAL, logger="jasper.active_speaker"
                         ".session_volume_plan"):
        monkey = plan._persist
        plan._persist = boom  # the marker write fails AFTER the fader is set
        try:
            result = asyncio.run(plan.close(set_only_the_floor, drain.get))
        finally:
            plan._persist = monkey

    assert result is plan_mod.SessionVolumeRestoreResult.EMERGENCY_ATTENUATED
    assert drain.state["value"] == float(floor)  # speaker IS at the floor
    assert "session_volume_persist_failed" in caplog.text

    # ...and the plan must now own nothing, so the next capture's hold cannot
    # command the declared volume back onto it.
    after = _fader(value=float(floor))
    assert asyncio.run(plan.hold_measurement_volume(after.set, after.get)) is None
    assert after.state["writes"] == []
    assert after.state["value"] == float(floor)
    with pytest.raises(plan_mod.SessionVolumePlanError):
        plan.assert_ready()


def test_the_hold_serializes_against_a_concurrent_drain(tmp_path):
    """The reason this lives on the plan and not at the capture seam.

    A ceiling enforcement and a capture's hold can be in flight at once — the
    ceiling is enforced from envelope reads, on a different call path. Without
    the shared restore lock the hold could read "ready", the drain could
    restore the household volume, and the hold could then write the
    measurement volume back onto a plan that no longer has an owner to restore
    it. Under the lock the hold either precedes the drain or sees the drained
    state, and this asserts the SECOND ordering: the fader ends at the
    household level, not above it.
    """
    plan, fader = _open_plan(tmp_path)
    fader.state["value"] = HOUSEHOLD_DB

    async def scenario() -> None:
        # Take the lock exactly as a drain does, start the hold, then drain.
        async with plan._restore_lock:  # modelling a live drain
            held = asyncio.ensure_future(
                plan.hold_measurement_volume(fader.set, fader.get)
            )
            await asyncio.sleep(0)
            assert not held.done(), "the hold must wait on the drain's lock"
            # What the drain does, minus its own re-entrant lock acquisition.
            plan._clear_resolved()
        assert await held is None

    asyncio.run(scenario())
    assert fader.state["writes"] == []
    assert fader.state["value"] == HOUSEHOLD_DB


# --------------------------------------------------------------------------- #
# end to end: one seam, both capture paths, refuse before any audio
# --------------------------------------------------------------------------- #


class _StubCam:
    """A CamillaDSP stand-in whose fader can be pinned against a setter."""

    def __init__(self, *, volume_db: float, accepts_writes: bool = True) -> None:
        self.volume_db = volume_db
        self.accepts_writes = accepts_writes
        self.volume_writes: list[float] = []

    async def get_volume_db(self, *, best_effort: bool = False) -> float:
        return self.volume_db

    async def set_volume_db(self, db: float, *, best_effort: bool = False) -> bool:
        self.volume_writes.append(float(db))
        if self.accepts_writes:
            self.volume_db = float(db)
        return True

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        return "/var/lib/camilladsp/configs/active_speaker_applied.yml"

    async def get_active_config_raw(self, *, best_effort: bool = False) -> str:
        return "devices: {samplerate: 48000}\n"


class _Window:
    async def __aenter__(self) -> "_Window":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Plan:
    """A session-volume plan stand-in for the SEAM tests.

    It shares the real plan's DELEGATION — the same :func:`hold_fader_at` — so
    these tests drive the real primitive, while staying free of the durable
    state file and of the real plan's loop-bound restore lock (which one
    ``asyncio.run`` per ``_drive`` call cannot share). It does NOT re-implement
    the real readiness rule; it is simply configured ready or not. The real
    plan's own readiness and locking are tested above, against the real
    ``SessionVolumePlan``.
    """

    def __init__(
        self,
        measurement_volume_db: float | None = DECLARED_DB,
        *,
        ready: bool = True,
    ) -> None:
        self.measurement_volume_db = measurement_volume_db
        self._ready = ready

    def assert_ready(self, now: Any = None) -> None:
        if not self._ready:
            from jasper.active_speaker.session_volume_plan import (
                SessionVolumePlanError,
            )

            raise SessionVolumePlanError(
                "a measurement volume is durably active but was not opened in "
                "this process (crash/restart); recover it and open a fresh "
                "session first"
            )

    async def hold_measurement_volume(
        self, set_main_volume_db, get_main_volume_db, *, context: str = "",
    ) -> float | None:
        if not self._ready or self.measurement_volume_db is None:
            return None
        return await hold_fader_at(
            self.measurement_volume_db,
            set_main_volume_db,
            get_main_volume_db,
            context=context,
        )


def _program() -> Any:
    return build_check_program(
        [
            RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
            RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
        ],
        ambient_s=0.5,
        pilot_duration_s=0.3,
    )


@pytest.fixture(autouse=True)
def _reset_volume_plan():
    yield
    v2host.set_volume_plan_for_tests(None)


def _drive(
    monkeypatch, tmp_path, *, phase: str, cam: _StubCam, plan: Any,
    played: list[str] | None = None,
    on_emit: Any = None,
) -> list[str]:
    """Play one phase through the real seam; return the audio that reached ALSA.

    Only the transport below the seams is faked, so the ordering under test —
    writer lock, graph load, fader hold, WAV handoff — is the production one.

    ``played`` may be supplied by the caller so the record SURVIVES a raise.
    That is the whole point on the refusal tests: "refuses before any audio" is
    an ORDERING claim, and a test that only catches the exception passes just
    as happily with the hold moved AFTER the stimulus.

    ``on_emit`` fires at the instant either branch hands its WAV to the
    transport, so a caller can sample what the speaker's level actually WAS
    when sound left — the only way to assert the loud direction rather than
    infer it from the write list.
    """
    if played is None:
        played = []

    def _emitted(tag: str) -> None:
        played.append(tag)
        if on_emit is not None:
            on_emit()
    from jasper.active_speaker import camilla_yaml as camilla_yaml_mod
    from jasper.active_speaker import crossover_v2_flow as flow_mod
    from jasper.active_speaker import program_playback as playback_mod
    from jasper.audio_measurement import program as program_mod
    from jasper.correction import coordinator

    monkeypatch.setattr(coordinator, "measurement_window", lambda **kw: _Window())
    monkeypatch.setattr(program_mod, "write_program_wav", lambda path, program: None)
    monkeypatch.setattr(
        camilla_yaml_mod, "emit_active_speaker_program_config",
        lambda *a, **k: "pipeline: []\n",
    )

    async def _fake_aplay(bundle_dir, artifact, *, alsa_device=None, timeout_s=60.0):
        _emitted("summed")
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(playback_mod, "verified_program_aplay", _fake_aplay)

    def _fake_seams(cam_arg, **kwargs):
        async def read_current_config_path() -> str:
            return await cam.get_config_file_path()

        async def load_program_graph(yaml_text: str) -> bool:
            return True

        async def restore_graph(path: str) -> bool:
            return True

        async def play_wav() -> Any:
            _emitted("routed")
            return SimpleNamespace(ok=True)

        async def readmit() -> Any:
            return SimpleNamespace(allowed=True, refusals=())

        return {
            "read_current_config_path": read_current_config_path,
            "load_program_graph": load_program_graph,
            "restore_graph": restore_graph,
            "play_wav": play_wav,
            "readmit": readmit,
            "writer_lock": lambda: _Window(),
        }

    monkeypatch.setattr(flow_mod, "bind_program_playback_seams", _fake_seams)
    v2host.set_volume_plan_for_tests(plan)

    play = v2host.bind_production_play(
        run_async=asyncio.run,
        camilla_factory=lambda: cam,
        evidence_store=SimpleNamespace(
            bundle_dir=tmp_path,
            identify_artifact=lambda rel: SimpleNamespace(fingerprint="f"),
        ),
        relay_session_id="drift_probe",
        topology=object(),
        preset=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:Test",
        safety_profile={},
        role_targets={},
        session_volume_db=DECLARED_DB,
    )
    play(phase, _program())
    return played


@pytest.mark.parametrize("phase", [PHASE_CHECK, PHASE_VERIFY])
def test_the_two_capture_paths_share_one_fader_hold(monkeypatch, tmp_path, phase):
    """THE PIN — the defect lived in the SPLIT between the two paths.

    The routed MEASURE path loads a program graph and the summed VERIFY path
    does not, so only the first carried a duck bracket whose release pulled the
    fader to the household level. Both reach the same hold anyway: a discipline
    that only one path runs is the shape that let a whole campaign's measure
    phases drift while its verify phases held.
    """
    cam = _StubCam(volume_db=HOUSEHOLD_DB)
    played = _drive(monkeypatch, tmp_path, phase=phase, cam=cam, plan=_Plan())
    assert played, "the capture must still play once the fader is proven"
    assert cam.volume_writes == [DECLARED_DB]
    assert cam.volume_db == DECLARED_DB


@pytest.mark.parametrize("phase", [PHASE_CHECK, PHASE_VERIFY])
def test_a_drifted_fader_refuses_the_capture_before_any_audio(
    monkeypatch, tmp_path, phase
):
    """A fader that will not hold refuses the capture rather than banking it.

    ``accepts_writes=False`` is the shape that matters: the setter reports
    success and the fader stays put — something else owns it.

    **"Nothing is emitted" is asserted, not merely implied by the raise.** The
    caller owns ``played`` so the record survives the exception: catching the
    refusal alone passes just as happily with the hold moved AFTER the
    stimulus, which is the mutation this whole file exists to fail.
    """
    played: list[str] = []
    cam = _StubCam(volume_db=HOUSEHOLD_DB, accepts_writes=False)
    with pytest.raises(MeasurementFaderDrift):
        _drive(
            monkeypatch, tmp_path, phase=phase, cam=cam, plan=_Plan(),
            played=played,
        )
    assert played == [], "the refusal must land BEFORE any audio, not after it"
    assert cam.volume_writes == [DECLARED_DB]  # it tried the repair first


@pytest.mark.parametrize("phase", [PHASE_CHECK, PHASE_VERIFY])
def test_an_undrifted_capture_still_plays(monkeypatch, tmp_path, phase):
    """The happy path is untouched: no write, no refusal, audio emitted."""
    cam = _StubCam(volume_db=DECLARED_DB)
    played = _drive(monkeypatch, tmp_path, phase=phase, cam=cam, plan=_Plan())
    assert played
    assert cam.volume_writes == []


@pytest.mark.parametrize("phase", [PHASE_CHECK, PHASE_VERIFY])
def test_no_declared_volume_discloses_and_does_not_police(
    monkeypatch, tmp_path, phase, caplog
):
    """A session that declared no measurement volume is a DIFFERENT defect with
    its own owner — ``SessionVolumePlan.assert_ready``, which ``play_program``
    already calls. This seam says so and stops there rather than growing a
    second gate on the same question."""
    cam = _StubCam(volume_db=HOUSEHOLD_DB)
    with caplog.at_level(logging.WARNING, logger="jasper.web.correction_crossover_v2"):
        played = _drive(
            monkeypatch, tmp_path, phase=phase, cam=cam,
            plan=_Plan(measurement_volume_db=None),
        )
    assert played
    assert cam.volume_writes == []
    assert "crossover_v2_capture_volume_unheld" in caplog.text


#: The campaign drifted QUIET, so every fixture above walks that direction.
#: This is the same walk reversed — a household sitting LOUDER than the
#: measurement volume — which is the direction that matters for hearing safety
#: and the one no banked evidence exercises.
LOUD_HOUSEHOLD_DB = -8.0


@pytest.mark.parametrize("phase", [PHASE_CHECK, PHASE_VERIFY])
def test_a_loud_household_is_pulled_down_before_any_audio(
    monkeypatch, tmp_path, phase
):
    """THE LOUD DIRECTION, at the seam rather than at the primitive.

    #2925's own account: "It was wrong quiet that night; the identical seam
    reversed is the LOUD direction." Whatever left the fader hot — a household
    level above the measurement volume, an authoritative UI/remote write, a
    racing writer — the stimulus must not be emitted at it, because the
    excitation ledger admitted the program against the DECLARED volume. Both
    branches must pull it down, and must do so before the WAV handoff — the
    ordering is asserted, not inferred, by recording the fader at the moment
    audio is emitted.

    This is the arm #2929 does NOT remove: the release reference makes the
    swap land on the declared level, and this hold still catches anything else
    that moved it.
    """
    seen_at_emit: list[float] = []
    cam = _StubCam(volume_db=LOUD_HOUSEHOLD_DB)

    played = _drive(
        monkeypatch, tmp_path, phase=phase, cam=cam, plan=_Plan(),
        on_emit=lambda: seen_at_emit.append(cam.volume_db),
    )

    assert played, "the capture must still happen once the level is proven"
    assert cam.volume_writes == [DECLARED_DB]
    # The fader was BELOW the household level at the instant audio was emitted.
    assert seen_at_emit == [DECLARED_DB]
    assert seen_at_emit[0] < LOUD_HOUSEHOLD_DB


@pytest.mark.parametrize("phase", [PHASE_CHECK, PHASE_VERIFY])
def test_a_loud_fader_that_will_not_come_down_emits_nothing(
    monkeypatch, tmp_path, phase
):
    """The refusal shape in the loud direction: the setter reports success and
    the fader stays hot. Nothing may be emitted at that level — this is the
    case the whole change exists to make impossible."""
    seen_at_emit: list[float] = []
    played: list[str] = []
    cam = _StubCam(volume_db=LOUD_HOUSEHOLD_DB, accepts_writes=False)

    with pytest.raises(MeasurementFaderDrift):
        _drive(
            monkeypatch, tmp_path, phase=phase, cam=cam, plan=_Plan(),
            played=played, on_emit=lambda: seen_at_emit.append(cam.volume_db),
        )

    assert played == []
    assert seen_at_emit == []
    assert cam.volume_db == LOUD_HOUSEHOLD_DB  # untouched, and unheard


@pytest.mark.parametrize("phase", [PHASE_CHECK, PHASE_VERIFY])
def test_an_unready_plan_never_gets_its_fader_raised(monkeypatch, tmp_path, phase):
    """THE SAFETY GUARD ON THE FIX ITSELF.

    A plan whose restore could not be CONFIRMED keeps ``measurement_volume_db``
    while latching ``unresolved`` — so reading that field unguarded would let
    this hold raise the fader BACK UP on a speaker whose plan had just given
    up. Which shape that is, precisely: a restore whose emergency write LANDED
    on the hardware but never read back at the floor. A restore that DOES
    confirm the floor returns ``EMERGENCY_ATTENUATED`` and clears the field to
    ``None`` via ``_clear_resolved`` — safe for a different reason (nothing
    left to hold), so it is not this test's subject. The declared volume is
    only ever taken from a plan ``assert_ready`` accepts.

    The two paths then answer differently, and both are correct: the routed
    path never reaches the hold at all, because ``play_program`` asks the same
    plan the same question first and refuses; the summed path has no such gate
    and keeps today's behaviour — it leaves the fader alone. Neither writes.
    """
    from jasper.active_speaker.session_volume_plan import SessionVolumePlanError

    cam = _StubCam(volume_db=HOUSEHOLD_DB)
    if phase == PHASE_VERIFY:
        assert _drive(
            monkeypatch, tmp_path, phase=phase, cam=cam, plan=_Plan(ready=False),
        )
    else:
        with pytest.raises(SessionVolumePlanError):
            _drive(
                monkeypatch, tmp_path, phase=phase, cam=cam, plan=_Plan(ready=False),
            )
    assert cam.volume_writes == []
    assert cam.volume_db == HOUSEHOLD_DB


# --------------------------------------------------------------------------- #
# the fix: the swap releases to the declared level BY CONSTRUCTION (#2929)
# --------------------------------------------------------------------------- #


async def _noop_confirm(_cam, _yaml_text) -> None:
    """The load seam's post-SetConfig readback proof, stubbed out.

    These tests are about the release reference a swap carries, not about
    graph confirmation — that is
    ``test_bind_program_playback_seams_uses_inline_setconfig``'s subject.
    """
    return None


def test_the_release_reference_is_the_plans_own_guarded_answer(tmp_path):
    """THE SAFETY GUARD ON THIS FIX.

    The reference the swap releases to is the same guarded question the hold
    asks, not the raw ``measurement_volume_db`` field — because
    ``_mark_unresolved`` copies that field forward, so a plan whose restore
    could not be confirmed still REPORTS the declared volume while owning
    nothing. Reading it unguarded would let a graph swap raise the fader back
    up on a speaker its session had just left at the emergency floor. Every
    state that makes the hold answer ``None`` must make this answer ``None``
    too, or the two disagree about who owns the fader.
    """
    plan, _fader_io = _open_plan(tmp_path)
    assert plan.owned_measurement_volume_db_nowait() == DECLARED_DB

    stuck = _fader(value=-5.0, sticks=False)
    asyncio.run(plan.close(stuck.set, stuck.get))
    assert plan.needs_recovery
    assert plan.measurement_volume_db == DECLARED_DB  # the field still says it
    assert plan.owned_measurement_volume_db_nowait() is None


def test_a_cleanly_closed_plan_offers_no_release_reference(tmp_path):
    """After a clean close the household owns the fader again, so a later swap
    must fall back to the canonical target rather than the measurement one."""
    plan, fader = _open_plan(tmp_path)
    asyncio.run(plan.close(fader.set, fader.get))
    assert plan.owned_measurement_volume_db_nowait() is None


def test_both_program_swaps_carry_the_declared_release_reference(monkeypatch, tmp_path):
    """The load AND the restore, not just the load.

    Releasing the RESTORE swap to the household level would simply move the
    drift one capture later: the next load reads its own entry snapshot from
    wherever the restore left the fader, so the hold would be repairing again
    from the second capture onward. Both swaps are asserted here because
    fixing only the obvious one looks green on a single-capture test.
    """
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    seen: list[tuple[str, float | None]] = []

    class _SpyCam:
        async def get_config_file_path(self, *, best_effort=False):
            return str(tmp_path / "entry.yml")

        async def set_active_config_raw(
            self, config, *, best_effort=False, held_target_db=None,
        ):
            answer = None if held_target_db is None else held_target_db()
            seen.append(("load" if "program" in config else "restore", answer))
            return True

    (tmp_path / "entry.yml").write_text("restore-me\n", encoding="utf-8")
    monkeypatch.setattr(flow_mod, "confirm_graph_is_live", _noop_confirm)

    def _owned() -> float:
        return DECLARED_DB

    seams = flow_mod.bind_program_playback_seams(
        _SpyCam(),
        bundle_dir=str(tmp_path),
        artifact=SimpleNamespace(fingerprint="f"),
        config_dir=str(tmp_path),
        program=_program(),
        wav_path=str(tmp_path / "p.wav"),
        topology=object(),
        safety_profile={},
        role_targets={},
        session_volume_db=DECLARED_DB,
        held_target_db=_owned,
    )

    assert asyncio.run(seams["load_program_graph"]("program: graph\n")) is True
    assert asyncio.run(seams["restore_graph"](str(tmp_path / "entry.yml"))) is True

    assert seen == [("load", DECLARED_DB), ("restore", DECLARED_DB)]


def test_a_drained_plan_stops_supplying_a_reference_mid_session(monkeypatch, tmp_path):
    """Read FRESH per swap, never captured once at bind time.

    A plan can drain mid-session (close, ceiling, a failed restore), and the
    swap must notice rather than keep referencing a level nobody owns. This
    pins the BETWEEN-swaps case; the drain that lands INSIDE one is
    ``test_a_drain_completing_inside_the_swap_is_not_undone_by_the_release``,
    and it is why the reader travels instead of its answer.
    """
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    seen: list[float | None] = []
    owned: list[float | None] = [DECLARED_DB]

    class _SpyCam:
        async def get_config_file_path(self, *, best_effort=False):
            return str(tmp_path / "entry.yml")

        async def set_active_config_raw(
            self, config, *, best_effort=False, held_target_db=None,
        ):
            seen.append(None if held_target_db is None else held_target_db())
            return True

    monkeypatch.setattr(flow_mod, "confirm_graph_is_live", _noop_confirm)

    def _owned() -> float | None:
        return owned[0]

    seams = flow_mod.bind_program_playback_seams(
        _SpyCam(),
        bundle_dir=str(tmp_path),
        artifact=SimpleNamespace(fingerprint="f"),
        config_dir=str(tmp_path),
        program=_program(),
        wav_path=str(tmp_path / "p.wav"),
        topology=object(),
        safety_profile={},
        role_targets={},
        session_volume_db=DECLARED_DB,
        held_target_db=_owned,
    )

    assert asyncio.run(seams["load_program_graph"]("program: graph\n")) is True
    owned[0] = None  # the plan drains
    assert asyncio.run(seams["load_program_graph"]("program: graph\n")) is True

    assert seen == [DECLARED_DB, None]


def test_no_reference_reader_leaves_every_swap_exactly_as_it_was(monkeypatch, tmp_path):
    """``held_target_db`` is optional, and omitting it must not start passing
    ``None`` in some new shape — it is the same call the seam always made."""
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    seen: list[float | None] = []

    class _SpyCam:
        async def get_config_file_path(self, *, best_effort=False):
            return str(tmp_path / "entry.yml")

        async def set_active_config_raw(
            self, config, *, best_effort=False, held_target_db=None,
        ):
            seen.append(None if held_target_db is None else held_target_db())
            return True

    monkeypatch.setattr(flow_mod, "confirm_graph_is_live", _noop_confirm)

    seams = flow_mod.bind_program_playback_seams(
        _SpyCam(),
        bundle_dir=str(tmp_path),
        artifact=SimpleNamespace(fingerprint="f"),
        config_dir=str(tmp_path),
        program=_program(),
        wav_path=str(tmp_path / "p.wav"),
        topology=object(),
        safety_profile={},
        role_targets={},
        session_volume_db=DECLARED_DB,
    )

    assert asyncio.run(seams["load_program_graph"]("program: graph\n")) is True
    assert seen == [None]


class _FakeCamillaClient:
    """CamillaDSP as it actually behaves (#2929).

    ``main_volume`` is PROCESS state and survives a config replace — the
    property ``_graph_mutation``'s whole fade-down/fade-back-up design already
    depended on, and the one #2925 mis-recorded as the opposite. v4.1.3's
    ``devices`` schema has no field that could carry a volume, so
    ``set_active_raw`` deliberately does not touch the fader here.
    """

    def __init__(self, volume_db: float) -> None:
        self._volume = float(volume_db)
        self.config = self
        self.volume = self
        self.general = self
        self.loads: list[str] = []

    def main_volume(self) -> float:
        return self._volume

    def set_main_volume(self, value: float) -> None:
        self._volume = float(value)

    def main_mute(self) -> bool:
        return False

    def set_active_raw(self, text: str) -> None:
        self.loads.append(text)


def _capture_sequence(monkeypatch, tmp_path, *, declare_reference: bool):
    """Drive three captures through the REAL duck and the REAL hold.

    Returns ``(writes_the_hold_made, fader_after_each_capture)``. Only the
    pycamilladsp client is faked, so the duck depth, the dwell, the release
    arithmetic and the hold are all production code.
    """
    from jasper import camilla as camilla_module
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeOpenResult,
        SessionVolumePlan,
    )

    monkeypatch.setattr(camilla_module, "MAIN_VOLUME_RAMP_SETTLE_S", 0.0)

    async def household() -> float:
        return HOUSEHOLD_DB

    monkeypatch.setattr(camilla_module, "_canonical_target_db_provider", household)

    fake = _FakeCamillaClient(HOUSEHOLD_DB)
    cam = camilla_module.CamillaController("127.0.0.1", 1234)
    cam._graph_mutation_lock_path = tmp_path / ".dsp_apply.lock"

    async def _call(fn):
        return fn(fake)

    cam._call = _call  # type: ignore[method-assign]

    hold_writes: list[float] = []

    async def set_v(db: float) -> bool:
        hold_writes.append(float(db))
        return await cam.set_volume_db(db, best_effort=False)

    async def get_v():
        return await cam.get_volume_db(best_effort=False)

    plan = SessionVolumePlan(state_path=tmp_path / "session_volume.json")

    async def _run():
        opened = await plan.open(DECLARED_DB, set_v, get_v)
        assert opened is SessionVolumeOpenResult.OPENED
        hold_writes.clear()  # the open's own write is not the hold's

        levels: list[float] = []
        for _ in range(3):
            reference = (
                plan.owned_measurement_volume_db_nowait
                if declare_reference
                else None
            )
            # 1. load the program graph, 2. prove the fader, 3. restore.
            await cam.set_active_config_raw(
                "program: graph\n", held_target_db=reference,
            )
            await plan.hold_measurement_volume(set_v, get_v, context="capture:check")
            levels.append(await cam.get_volume_db())
            await cam.set_active_config_raw(
                "entry: graph\n", held_target_db=reference,
            )
        return levels

    return hold_writes, asyncio.run(_run())


def test_a_measurement_session_leaves_the_hold_nothing_to_repair(
    monkeypatch, tmp_path,
):
    """THE POINT OF #2929, end to end: zero writes across a multi-capture run.

    The hold is promoted from a repair to a tripwire. Every stimulus is still
    emitted at the declared level — that part is unchanged and is what the
    excitation ledger requires — but the level is now arrived at by
    construction, so the hold reads it, agrees, and writes nothing.
    """
    hold_writes, levels = _capture_sequence(
        monkeypatch, tmp_path, declare_reference=True,
    )

    assert hold_writes == []
    assert levels == [DECLARED_DB, DECLARED_DB, DECLARED_DB]


def test_without_the_reference_the_hold_repairs_every_capture(monkeypatch, tmp_path):
    """THE CONTROL, and the mutation proof for the test above.

    Drop the release reference and the overnight defect reappears exactly:
    the swap releases to the household level, and the hold has to drag the
    fader back before every single stimulus. This is what
    ``repairing``/``repaired`` on every routed capture actually meant.
    """
    hold_writes, levels = _capture_sequence(
        monkeypatch, tmp_path, declare_reference=False,
    )

    assert hold_writes == [DECLARED_DB, DECLARED_DB, DECLARED_DB]
    assert levels == [DECLARED_DB, DECLARED_DB, DECLARED_DB]


def test_a_fader_moved_by_something_else_still_refuses_fail_closed(
    monkeypatch, tmp_path,
):
    """THE TRIPWIRE KEEPS ITS TEETH.

    Correct-by-construction is not a reason to stop checking. A fader that
    something else is holding — the setter reports success and nothing moves —
    must still refuse the capture, because a stimulus at an unproven level was
    never the one the ledger admitted.
    """
    plan, _ = _open_plan(tmp_path)
    stuck = _fader(value=HOUSEHOLD_DB, sticks=False)

    with pytest.raises(MeasurementFaderDrift):
        asyncio.run(
            plan.hold_measurement_volume(
                stuck.set, stuck.get, context="capture:check",
            )
        )


# --------------------------------------------------------------------------- #
# the mid-swap drain window (#2929 gate panel, found independently by both lenses)
# --------------------------------------------------------------------------- #


def _swap_with_drain_inside(monkeypatch, tmp_path, *, hold_lock: bool):
    """Complete a drain INSIDE the duck bracket, then let the release run.

    Deterministic, not timing-dependent: the drain is driven from the fake
    client's own config-apply call, which is exactly the point between the duck
    and the release. That is the window the remote driver's ~1.5 s
    ``/crossover/status`` poll can land a wall-clock-ceiling drain in.

    ``hold_lock`` picks which half of the window is probed — a drain still
    RUNNING at release time (lock held) versus one that already COMPLETED
    (lock free, state cleared).
    """
    from jasper import camilla as camilla_module
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeOpenResult,
        SessionVolumePlan,
    )

    monkeypatch.setattr(camilla_module, "MAIN_VOLUME_RAMP_SETTLE_S", 0.0)

    async def household() -> float:
        return HOUSEHOLD_DB

    monkeypatch.setattr(camilla_module, "_canonical_target_db_provider", household)

    fake = _FakeCamillaClient(HOUSEHOLD_DB)
    cam = camilla_module.CamillaController("127.0.0.1", 1234)
    cam._graph_mutation_lock_path = tmp_path / ".dsp_apply.lock"
    plan = SessionVolumePlan(state_path=tmp_path / "session_volume.json")

    async def set_v(db: float) -> bool:
        return await cam.set_volume_db(db, best_effort=False)

    async def get_v():
        return await cam.get_volume_db(best_effort=False)

    drained: list[str] = []

    async def _call(fn):
        # The rendezvous: fires only for the config apply, i.e. mid-bracket —
        # after the duck, before the release. Every other call is a plain
        # pass-through, including the ones `plan.open` makes below.
        if getattr(fn, "__mid_swap__", False):
            if hold_lock:
                await plan._restore_lock.acquire()
                drained.append("lock_held")
            else:
                await plan.close(set_v, get_v)
                drained.append("closed")
        return fn(fake)

    cam._call = _call  # type: ignore[method-assign]

    async def _run():
        opened = await plan.open(DECLARED_DB, set_v, get_v)
        assert opened is SessionVolumeOpenResult.OPENED
        assert await cam.get_volume_db() == DECLARED_DB

        def _apply(c):
            return c.config.set_active_raw("program: graph\n")

        _apply.__mid_swap__ = True  # type: ignore[attr-defined]
        # Drive the bracket directly so the rendezvous rides the real duck.
        async with cam._graph_mutation(
            "test", held_target_db=plan.owned_measurement_volume_db_nowait,
        ):
            await cam._call(_apply)
        if hold_lock:
            plan._restore_lock.release()
        return await cam.get_volume_db()

    final_db = asyncio.run(_run())
    assert drained, "the rendezvous never fired — the probe proves nothing"
    return final_db


def test_a_drain_completing_inside_the_swap_is_not_undone_by_the_release(
    monkeypatch, tmp_path,
):
    """THE TOCTOU BOTH LENSES FOUND, pinned.

    A wall-clock-ceiling drain can complete between the duck and the release.
    It restores the household volume and reports a confirmed restore — and a
    release still carrying a reference resolved BEFORE the swap would put the
    measurement level straight back on a speaker with no owner left to take it
    down again (measured +13.21 dB by the safety lens against a household
    drain). Asking the reader AT release time is what closes it: the drained
    plan answers None and the release takes the canonical household target.
    """
    final_db = _swap_with_drain_inside(monkeypatch, tmp_path, hold_lock=False)

    assert final_db == pytest.approx(HOUSEHOLD_DB)


def test_a_drain_still_running_at_release_time_reads_as_unowned(
    monkeypatch, tmp_path,
):
    """The other half of the window, and why the peek is non-blocking.

    A drain holding the restore lock is mid-flight; its outcome is not knowable
    here without waiting for it, and waiting is forbidden — this runs inside a
    shielded ``finally``, so a wedged drain would strand a ducked (silent)
    speaker. Reading that as unowned is the fail-safe answer: the release falls
    back to canonical, exactly as a non-measurement swap does.
    """
    final_db = _swap_with_drain_inside(monkeypatch, tmp_path, hold_lock=True)

    assert final_db == pytest.approx(HOUSEHOLD_DB)


def test_the_release_reader_is_never_awaited_and_never_waits(monkeypatch, tmp_path):
    """The contract that keeps the shielded finally safe.

    Two properties, both load-bearing and neither visible from behaviour alone:
    the reader is SYNC (an awaitable would need an await inside the finally),
    and it does not block on the plan's lock (a wedged drain must not strand a
    ducked speaker). A future refactor that makes either one false is what this
    catches.
    """
    import asyncio as _asyncio
    import inspect

    from jasper.active_speaker.session_volume_plan import SessionVolumePlan

    reader = SessionVolumePlan.owned_measurement_volume_db_nowait
    assert not inspect.iscoroutinefunction(reader)

    plan, _fader = _open_plan(tmp_path)

    async def _locked_read():
        await plan._restore_lock.acquire()
        try:
            # Would deadlock, not return, if this waited for the lock.
            return await _asyncio.wait_for(
                _asyncio.to_thread(plan.owned_measurement_volume_db_nowait),
                timeout=5.0,
            )
        finally:
            plan._restore_lock.release()

    assert asyncio.run(_locked_read()) is None


def test_the_in_tolerance_hold_leaves_a_positive_liveness_line(tmp_path, caplog):
    """THE ACCEPTANCE CRITERION'S OTHER HALF (#2929 gate, safety SF3).

    "No repair pair" is only evidence if a hold that never RAN looks different
    from one that ran and agreed. Absence cannot carry that on its own — the
    #2198 instrument-silence lesson — so the in-tolerance path says so.
    """
    plan, fader = _open_plan(tmp_path)

    with caplog.at_level(logging.INFO, logger=LATCH_LOGGER):
        assert asyncio.run(
            plan.hold_measurement_volume(fader.set, fader.get, context="capture:check")
        ) == DECLARED_DB

    assert fader.state["writes"] == []  # still zero writes
    held = [r for r in caplog.records if "result=held" in r.getMessage()]
    assert len(held) == 1, "the in-tolerance hold must prove it ran"
    assert "context=capture:check" in held[0].getMessage()
    assert "event=active_speaker.measurement_fader_drift" in held[0].getMessage()
