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

* **T1-1** — the play path PROVES the declared measurement volume per
  stimulus, and refuses the capture when it cannot. It reads; it never writes.
  The safety framing is not measurement tidiness: ``readmit_program_from_wav``
  admitted every program against the DECLARED volume, so the excitation-safety
  ledger was wrong for every capture that night. It was wrong QUIET; the
  identical seam reversed is LOUD.
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
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_MEASUREMENT_VOLUME_DRIFT,
    REASON_REGISTRY,
)
from jasper.active_speaker.session_volume_plan import FaderVolumeDoor
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


def _fader(*, value: Any, sticks: bool = True) -> Any:
    """A fader stand-in: one live value, plus a setter that may not stick.

    The setter is for the plan's own drains (``open``/``close``), which do
    write. The hold does not — it takes the getter alone.

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
        if sticks:
            state["value"] = float(db)
        return True

    return SimpleNamespace(get=get_db, set=set_db, state=state)


def test_an_undrifted_fader_costs_one_read_and_no_write():
    """The happy path is the common path: read, agree, done. Nothing is
    written, so the hold cannot itself become a source of volume churn."""
    fader = _fader(value=DECLARED_DB)
    proven = asyncio.run(hold_fader_at(DECLARED_DB, fader.get, context="check"))
    assert proven == DECLARED_DB
    assert fader.state["writes"] == []
    assert fader.state["reads"] == 1


def test_a_drifted_fader_is_disclosed_and_then_refused_without_a_write(caplog):
    """The T1-1 fix as wave 5 leaves it: prove, disclose, refuse.

    A fader left off the declared level is DISCLOSED — a silent disagreement is
    how a whole campaign of stimuli can play at the wrong level without one
    line of evidence — and then refused, because establishing the level is
    ``SessionVolumePlan.open``'s job and a per-stimulus repair here was a
    SECOND writer moving the fader behind the session's back. Nothing is
    written: the fader is left exactly where the refusal found it.
    """
    fader = _fader(value=HOUSEHOLD_DB)
    with caplog.at_level(logging.WARNING, logger=LATCH_LOGGER):
        with pytest.raises(MeasurementFaderDrift):
            asyncio.run(
                hold_fader_at(DECLARED_DB, fader.get, context="capture:check")
            )
    assert fader.state["writes"] == []
    assert fader.state["value"] == HOUSEHOLD_DB  # untouched, not dragged down
    assert "result=disagreed" in caplog.text
    # Both values named, not just the delta — a forensic reader needs to know
    # WHICH of the two is the surprise.
    assert f"observed_db={HOUSEHOLD_DB:.6f}" in caplog.text
    assert f"expected_db={DECLARED_DB:.6f}" in caplog.text


@pytest.mark.parametrize(
    ("value", "why", "observed"),
    [
        # Read fine, and not where the session declared it — something else is
        # holding it, and JTS does not fight for the fader.
        (HOUSEHOLD_DB, "held elsewhere", HOUSEHOLD_DB),
        # Unreadable is equally unprovable, and an unprovable level is not a
        # measurement. (A fader that answers the independent re-read is a
        # different case and is deliberately allowed through — see below.)
        (None, "unreadable", None),
    ],
)
def test_an_unprovable_fader_refuses_rather_than_returning(value, why, observed):
    """...and reports the observation it ACTUALLY made in each case.

    ``observed_db`` is the discriminator: an empty value means "could not
    read". It only
    discriminates because the proving re-read is unconditional and independent:
    the refusal names a reading JTS actually took, not one inferred from an
    earlier round-trip. That is the ``locate_failed`` #2085 class — copy
    stating an observation JTS never made — inside the change that exists to
    close it.
    """
    fader = _fader(value=value)
    with pytest.raises(MeasurementFaderDrift) as raised:
        asyncio.run(
            hold_fader_at(DECLARED_DB, fader.get, context="capture:check")
        )
    assert fader.state["writes"] == []
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
            asyncio.run(hold_fader_at(DECLARED_DB, fader.get))
    line = next(
        rec.getMessage()
        for rec in caplog.records
        if "result=refused" in rec.getMessage()
    )
    return dict(token.split("=", 1) for token in line.split() if "=" in token)


def test_an_empty_observed_db_means_unreadable_and_nothing_else(caplog):
    """The one clean discriminator on the refusal line, asserted as a
    biconditional rather than in one direction: a fader that could not be read
    reports empty, and a fader that read and is simply not at the declared
    level reports the value it read. It is a reading JTS took, never an
    inference, because the proving re-read is unconditional."""
    wrong = _refusal_fields(caplog, _fader(value=HOUSEHOLD_DB))
    unreadable = _refusal_fields(caplog, _fader(value=None))

    assert unreadable["observed_db"] == '""'
    assert wrong["observed_db"] == f"{HOUSEHOLD_DB:.6f}"


def test_a_transiently_unreadable_fader_is_proven_by_the_re_read_not_refused():
    """Disclose, never force. A read that misses once and answers on the
    INDEPENDENT re-read has still proven the level, so the capture proceeds —
    the refusal is for a level that cannot be established, not for one
    round-trip that missed. Nothing moved the fader in between; the second read
    simply found what the first could not."""
    answers: list[Any] = [None, DECLARED_DB]

    async def get_db() -> Any:
        return answers.pop(0)

    assert asyncio.run(hold_fader_at(DECLARED_DB, get_db)) == DECLARED_DB
    assert answers == [], "both reads must be taken, and only two"


def test_a_wedged_fader_read_refuses_instead_of_raising_its_own_error():
    """``CamillaUnavailable`` reaches this seam as ``RuntimeError`` (the
    caller's ``_session_volume_read`` wraps it). It must land as the refusal,
    not as an unclassified error the catch-all arm calls ``internal_error``."""

    async def boom() -> float:
        raise RuntimeError("CamillaDSP is unavailable")

    with pytest.raises(MeasurementFaderDrift):
        asyncio.run(hold_fader_at(DECLARED_DB, boom))


# --------------------------------------------------------------------------- #
# the refusal reaches the household as its own honest code
# --------------------------------------------------------------------------- #


def test_the_drift_refusal_is_classified_as_its_own_reason():
    """Never ``program_unplayable``: that code's copy says JTS "could not play
    the measurement signal within the speaker's safe limits", which is the
    opposite of what happened — the program was admissible and the SPEAKER's
    level was not the one it was admitted against (the #1820 rule that a
    refusal needing a different action needs a different code)."""
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_PROGRAM_UNPLAYABLE,
    )

    classified = v2host.classify_program_failure(
        MeasurementFaderDrift(
            expected_db=DECLARED_DB, observed_db=HOUSEHOLD_DB, context="capture:check"
        )
    )
    assert classified == (REASON_MEASUREMENT_VOLUME_DRIFT, ())
    assert REASON_MEASUREMENT_VOLUME_DRIFT != REASON_PROGRAM_UNPLAYABLE


def test_the_drift_reason_is_terminal_and_not_retried_around():
    """Campaign guardrail 2, verbatim: "a safety refusal is not something to
    retry around." The level has already been read twice, independently, and
    could not be proven, so another attempt at the same capture only re-reads
    whatever is holding the fader."""
    from jasper.active_speaker.crossover_v2.refusal_copy import TEMPLATE_HARD_STOP

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
    assert asyncio.run(hold_fader_at(DECLARED_DB, inside.get)) == just_inside
    assert inside.state["writes"] == []

    outside = _fader(value=just_outside)
    with pytest.raises(MeasurementFaderDrift):
        asyncio.run(hold_fader_at(DECLARED_DB, outside.get))


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
    opened = asyncio.run(plan.open(at_db, FaderVolumeDoor(fader.set, fader.get)))
    assert opened is SessionVolumeOpenResult.OPENED
    fader.state["writes"].clear()
    return plan, fader


def test_the_plan_holds_its_own_declared_volume(tmp_path):
    """The stateful seam: the plan proves against the volume IT opened, so the
    capture path never has to carry a second copy of that number — and it
    returns the reading it actually TOOK, not the target it checked against.
    The two differ here by less than the tolerance, which is the only way to
    tell those apart."""
    plan, fader = _open_plan(tmp_path)
    fader.state["value"] = DECLARED_DB + 0.8 * READBACK_TOLERANCE_DB
    assert asyncio.run(
        plan.hold_measurement_volume(fader.get, context="capture:check")
    ) == fader.state["value"]
    assert fader.state["writes"] == []


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
    asyncio.run(plan.close(FaderVolumeDoor(stuck.set, stuck.get)))
    assert plan.needs_recovery
    assert plan.measurement_volume_db == DECLARED_DB  # the field still says it

    after = _fader(value=HOUSEHOLD_DB)
    assert asyncio.run(
        plan.hold_measurement_volume(after.get)
    ) is None
    assert after.state["writes"] == []


def test_a_restored_plan_holds_nothing(tmp_path):
    """After a clean close there is no volume to hold, so the household's own
    level is left exactly where the restore put it."""
    plan, fader = _open_plan(tmp_path)
    asyncio.run(plan.close(FaderVolumeDoor(fader.set, fader.get)))
    fader.state["writes"].clear()
    assert asyncio.run(plan.hold_measurement_volume(fader.get)) is None
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
        plan.open(DECLARED_DB, FaderVolumeDoor(fader.set, fader.get))
    ) is SessionVolumeOpenResult.OPENED
    fader.state["value"] = HOUSEHOLD_DB
    fader.state["writes"].clear()

    now[0] = 1000.0 + 11.0  # past the ceiling
    assert plan.stale_active()
    assert asyncio.run(plan.hold_measurement_volume(fader.get)) is None
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
            result = asyncio.run(plan.close(FaderVolumeDoor(set_only_the_floor, drain.get)))
        finally:
            plan._persist = monkey

    assert result is plan_mod.SessionVolumeRestoreResult.EMERGENCY_ATTENUATED
    assert drain.state["value"] == float(floor)  # speaker IS at the floor
    assert "session_volume_persist_failed" in caplog.text

    # ...and the plan must now own nothing, so the next capture's hold cannot
    # command the declared volume back onto it.
    after = _fader(value=float(floor))
    assert asyncio.run(plan.hold_measurement_volume(after.get)) is None
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
                plan.hold_measurement_volume(fader.get)
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
    """A CamillaDSP stand-in with one live fader, and a setter nothing calls.

    ``volume_writes`` is the instrument: the hold reads, so an empty list is
    the assertion that this seam did not grow a second writer back.
    """

    def __init__(self, *, volume_db: float) -> None:
        self.volume_db = volume_db
        self.volume_writes: list[float] = []

    async def get_volume_db(self, *, best_effort: bool = False) -> float:
        return self.volume_db

    async def set_volume_db(self, db: float, *, best_effort: bool = False) -> bool:
        self.volume_writes.append(float(db))
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
        self, get_main_volume_db, *, context: str = "",
    ) -> float | None:
        if not self._ready or self.measurement_volume_db is None:
            return None
        return await hold_fader_at(
            self.measurement_volume_db, get_main_volume_db, context=context,
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
    from jasper.active_speaker.crossover_v2 import composition as composition_mod
    from jasper.active_speaker import program_playback as playback_mod
    from jasper.active_speaker.crossover_v2 import session_graph as session_graph_mod
    from jasper.audio_measurement import program as program_mod
    from jasper import measurement_window as coordinator

    monkeypatch.setattr(coordinator, "measurement_window", lambda **kw: _Window())
    monkeypatch.setattr(program_mod, "write_program_wav", lambda path, program: None)
    monkeypatch.setattr(
        camilla_yaml_mod, "emit_active_speaker_program_config",
        lambda *a, **k: "pipeline: []\n",
    )

    # The session's measurement graph is stubbed out because these tests are
    # about the FADER HOLD both playback paths share, not about the graph
    # transport — which has its own suite
    # (``tests/test_crossover_v2_session_graph.py``). Leaving it real would put
    # the production DSP writer lock and a CamillaDSP swap inside a probe whose
    # instrument is ``volume_writes``.
    async def _no_install(self) -> str:
        return "stub"

    async def _no_restore(self) -> None:
        return None

    monkeypatch.setattr(
        session_graph_mod.MeasurementSessionGraph, "install", _no_install
    )
    monkeypatch.setattr(
        session_graph_mod.MeasurementSessionGraph, "restore", _no_restore
    )

    async def _fake_aplay(bundle_dir, artifact, *, alsa_device=None, timeout_s=60.0):
        _emitted("summed")
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(playback_mod, "verified_program_aplay", _fake_aplay)

    def _fake_seams(cam_arg, **kwargs):
        async def play_wav() -> Any:
            _emitted("routed")
            return SimpleNamespace(ok=True)

        async def readmit() -> Any:
            return SimpleNamespace(allowed=True, refusals=())

        return {
            "play_wav": play_wav,
            "readmit": readmit,
            "writer_lock": lambda: _Window(),
        }

    monkeypatch.setattr(composition_mod, "bind_program_playback_seams", _fake_seams)
    v2host.set_volume_plan_for_tests(plan)

    play = v2host.bind_production_play(
        run_async=asyncio.run,
        camilla_factory=lambda: cam,
        evidence_store=SimpleNamespace(
            bundle_dir=tmp_path,
            identify_artifact=lambda rel: SimpleNamespace(fingerprint="f"),
        ),
        capture_session_id="drift_probe",
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
def test_the_two_capture_paths_share_one_fader_hold(
    monkeypatch, tmp_path, phase, caplog
):
    """THE PIN — the defect lived in the SPLIT between the two paths.

    The routed MEASURE path loads a program graph and the summed VERIFY path
    does not, so only the first carried a duck bracket whose release pulled the
    fader to the household level. Both reach the same hold anyway: a discipline
    that only one path runs is the shape that let a whole campaign's measure
    phases drift while its verify phases held.

    The hold writes nothing now, so a healthy capture's only trace is the
    liveness line — which is exactly what it is for (#2198): without it, "both
    paths ran the hold" and "neither did" look identical from here.
    """
    cam = _StubCam(volume_db=DECLARED_DB)
    with caplog.at_level(logging.INFO, logger=LATCH_LOGGER):
        played = _drive(monkeypatch, tmp_path, phase=phase, cam=cam, plan=_Plan())

    assert played, "the capture must still play once the fader is proven"
    assert cam.volume_writes == []
    held = [r for r in caplog.records if "result=held" in r.getMessage()]
    assert len(held) == 1, "this path must run the hold exactly once"
    assert f"context=capture:{phase}" in held[0].getMessage()


@pytest.mark.parametrize("phase", [PHASE_CHECK, PHASE_VERIFY])
def test_a_drifted_fader_refuses_the_capture_before_any_audio(
    monkeypatch, tmp_path, phase
):
    """A fader that cannot be proven refuses the capture rather than banking it.

    **"Nothing is emitted" is asserted, not merely implied by the raise.** The
    caller owns ``played`` so the record survives the exception: catching the
    refusal alone passes just as happily with the hold moved AFTER the
    stimulus, which is the mutation this whole file exists to fail.
    """
    played: list[str] = []
    cam = _StubCam(volume_db=HOUSEHOLD_DB)
    with pytest.raises(MeasurementFaderDrift):
        _drive(
            monkeypatch, tmp_path, phase=phase, cam=cam, plan=_Plan(),
            played=played,
        )
    assert played == [], "the refusal must land BEFORE any audio, not after it"
    assert cam.volume_writes == []  # it refuses; it does not fight for the fader
    assert cam.volume_db == HOUSEHOLD_DB


@pytest.mark.parametrize("phase", [PHASE_CHECK, PHASE_VERIFY])
def test_an_undrifted_capture_still_plays(monkeypatch, tmp_path, phase):
    """The happy path is untouched: no write, no refusal, audio emitted.

    This is also what proves the ``on_emit`` sampler the refusal tests rely on
    is LIVE — an empty ``seen_at_emit`` over there is only evidence if a
    capture that does emit fills it (#2198).
    """
    seen_at_emit: list[float] = []
    cam = _StubCam(volume_db=DECLARED_DB)
    played = _drive(
        monkeypatch, tmp_path, phase=phase, cam=cam, plan=_Plan(),
        on_emit=lambda: seen_at_emit.append(cam.volume_db),
    )
    assert played
    assert cam.volume_writes == []
    assert seen_at_emit == [DECLARED_DB]


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
def test_a_loud_fader_emits_no_audio_and_is_never_written_down(
    monkeypatch, tmp_path, phase
):
    """THE LOUD DIRECTION, at the seam rather than at the primitive.

    #2925's own account: "It was wrong quiet that night; the identical seam
    reversed is the LOUD direction." Whatever left the fader hot — a household
    level above the measurement volume, an authoritative UI/remote write, a
    racing writer — the stimulus must not be emitted at it, because the
    excitation ledger admitted the program against the DECLARED volume. Both
    branches must refuse, and must do so before the WAV handoff — the ordering
    is asserted, not inferred, by recording the fader at the moment audio is
    emitted.

    **The hold is not what brings a loud household down**; establishing the
    measurement volume is ``SessionVolumePlan.open``'s one set-and-confirm.
    A per-stimulus pull-down here was a second writer, and its absence is
    asserted: no write is attempted, and no audio leaves at the hot level.
    """
    seen_at_emit: list[float] = []
    played: list[str] = []
    cam = _StubCam(volume_db=LOUD_HOUSEHOLD_DB)

    with pytest.raises(MeasurementFaderDrift):
        _drive(
            monkeypatch, tmp_path, phase=phase, cam=cam, plan=_Plan(),
            played=played, on_emit=lambda: seen_at_emit.append(cam.volume_db),
        )

    assert played == [], "no stimulus may be emitted above the declared volume"
    assert seen_at_emit == []
    assert cam.volume_writes == []


@pytest.mark.parametrize("phase", [PHASE_CHECK, PHASE_VERIFY])
def test_a_loud_fader_that_will_not_come_down_emits_nothing(
    monkeypatch, tmp_path, phase
):
    """The other half of the loud direction: what happens to the FADER.

    Nothing comes down, because nothing here writes — so the speaker is left
    exactly as hot as it was found, and the only protection is that no audio
    was ever handed to it. The test above owns "nothing was emitted"; this one
    owns "and the level was left alone", which is the state a household is
    actually sitting in after the refusal.
    """
    played: list[str] = []
    cam = _StubCam(volume_db=LOUD_HOUSEHOLD_DB)

    with pytest.raises(MeasurementFaderDrift):
        _drive(
            monkeypatch, tmp_path, phase=phase, cam=cam, plan=_Plan(),
            played=played,
        )

    assert played == []
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



def test_the_in_tolerance_hold_leaves_a_positive_liveness_line(tmp_path, caplog):
    """THE ACCEPTANCE CRITERION'S OTHER HALF (#2929 gate, safety SF3).

    "No disagreement line" is only evidence if a hold that never RAN looks
    different from one that ran and agreed. Absence cannot carry that on its
    own — the #2198 instrument-silence lesson — so the in-tolerance path says
    so. It is the only trace a healthy capture leaves now that the hold writes
    nothing at all.
    """
    plan, fader = _open_plan(tmp_path)

    with caplog.at_level(logging.INFO, logger=LATCH_LOGGER):
        assert asyncio.run(
            plan.hold_measurement_volume(fader.get, context="capture:check")
        ) == DECLARED_DB

    assert fader.state["writes"] == []  # still zero writes
    held = [r for r in caplog.records if "result=held" in r.getMessage()]
    assert len(held) == 1, "the in-tolerance hold must prove it ran"
    assert "context=capture:check" in held[0].getMessage()
    assert "event=active_speaker.measurement_fader_drift" in held[0].getMessage()
