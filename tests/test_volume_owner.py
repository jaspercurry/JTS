# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One owner arbitrates the main fader; 18 writers had nothing between them.

The subject is ``jasper.volume_owner``. What is pinned here is the arbitration
a fader with 18 writers never had: which claim wins, what a release lands on,
and when a level counts as proven. The 0 dB ceiling is deliberately NOT pinned
here — it belongs to ``jasper.camilla._coerce_main_volume_db`` and its own
suite, and the property this file asserts instead is that the owner reaches the
fader only through the injected door, so that clamp cannot be routed around.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from jasper.active_speaker.volume_latch import READBACK_TOLERANCE_DB
from jasper.volume_owner import (
    ClaimKind,
    VolumeClaimConflict,
    VolumeClaimRefused,
    VolumeOwner,
    duck_release_target_db,
    install_volume_owner,
    volume_owner,
)

HOUSEHOLD_DB = -21.212124
MEASUREMENT_DB = -12.5


class _DoorRaised(Exception):
    """A door that raises something the fail-closed set does not name.

    ``CamillaUnavailable`` is exactly this shape: an ``Exception`` outside
    :data:`~jasper.active_speaker.volume_latch.FADER_IO_ERRORS`, which
    ``set_and_confirm_volume`` therefore does not swallow. Holders are
    contracted to bind ``best_effort=True`` doors so it never escapes; this is
    what happens when one does not.
    """


class _Fader:
    """A fader that remembers every write, and can refuse or lie."""

    def __init__(self, db: float | None = -30.0) -> None:
        self.db = db
        self.writes: list[float] = []
        self.accept = True
        self.lies = False
        self.ceiling: float | None = None
        self.readable = True
        self.raise_on_read = False
        self.raise_on_write = False

    async def set(self, db: float) -> bool:
        self.writes.append(float(db))
        if self.raise_on_write:
            raise _DoorRaised("door is not best-effort bound")
        if not self.accept:
            return False
        if not self.lies:
            self.db = float(db)
            if self.ceiling is not None:
                self.db = min(self.db, self.ceiling)
        return True

    async def get(self) -> float | None:
        if self.raise_on_read:
            raise RuntimeError("fader unreachable")
        return self.db if self.readable else None


def _owner(fader: _Fader) -> VolumeOwner:
    # Looked up at CALL time, not captured: a test that swaps a door mid-flight
    # must actually be swapping the door the owner uses. Production binds the
    # same way, through the coordinator's own methods.
    return VolumeOwner(
        set_fader_db=lambda db: fader.set(db),
        get_fader_db=lambda: fader.get(),
    )


async def _household(fader: _Fader) -> VolumeOwner:
    owner = _owner(fader)
    assert await owner.declare_household_level_db(HOUSEHOLD_DB) is True
    return owner


# --- the ranked claim -------------------------------------------------------


@pytest.mark.parametrize(
    "kind", [ClaimKind.SESSION_MEASUREMENT, ClaimKind.COMMISSIONING],
)
async def test_a_claim_that_outranks_household_takes_the_fader(kind):
    fader = _Fader()
    owner = await _household(fader)

    await owner.acquire_level(kind, MEASUREMENT_DB)

    assert owner.declared_level_db() == MEASUREMENT_DB
    assert fader.db == MEASUREMENT_DB


async def test_household_declared_under_a_claim_records_but_never_writes():
    """The defect this rank exists to close: a household twist mid-stimulus."""
    fader = _Fader()
    owner = await _household(fader)
    await owner.acquire_level(ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB)
    fader.writes.clear()

    assert await owner.declare_household_level_db(-3.0) is True

    assert fader.writes == []
    assert owner.declared_level_db() == MEASUREMENT_DB


@pytest.mark.parametrize("declared", [-30.0, -3.0])
async def test_the_release_lands_on_the_household_level_declared_meanwhile(
    declared,
):
    """A deferred household intent is not lost — it is what the release gives.

    Both directions: the twist that lands mid-session may be quieter or louder
    than the level the session was holding.
    """
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )
    await owner.declare_household_level_db(declared)

    await owner.release(claim)

    assert fader.db == declared


@pytest.mark.parametrize("level", [MEASUREMENT_DB, -60.0])
async def test_a_level_release_restores_outright_and_is_not_clamped_to_now(
    level,
):
    """Level releases are not duck releases: clamping strands the fader.

    ``min(reference, current)`` is right for a duck, which may give back only
    what it took, and wrong here in BOTH directions — a claim held below the
    household level (the emergency floor is one) would keep the speaker at the
    level being given up rather than handing it back.
    """
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(ClaimKind.SESSION_MEASUREMENT, level)

    await owner.release(claim)

    assert fader.db == HOUSEHOLD_DB


async def test_commissioning_outranks_a_live_measurement_claim():
    fader = _Fader()
    owner = await _household(fader)
    measurement = await owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )

    await owner.acquire_level(ClaimKind.COMMISSIONING, -6.0)

    assert owner.declared_level_db() == -6.0
    assert await owner.prove(measurement) is None


async def test_a_claim_recorded_under_a_higher_one_is_held_without_writing():
    """A claim that only asked to be RECORDED cannot fail to be established.

    It never asked for the fader, so a fader that cannot be written must not
    refuse it — the level it declares is what the higher claim's release will
    land on, and losing it there is how a measurement ends at the wrong level.

    This is the ONE thing that separates the rank short-circuit from letting
    the settle re-derive the same target: the target is identical either way,
    so only the failure path can tell them apart. The fader below is both
    drifted AND unwritable, which is what makes the difference observable.
    """
    fader = _Fader()
    owner = await _household(fader)
    await owner.acquire_level(ClaimKind.COMMISSIONING, -6.0)
    fader.db = -20.0
    fader.accept = False
    fader.writes.clear()

    claim = await owner.acquire_level(ClaimKind.SESSION_MEASUREMENT, -12.0)

    assert owner.holds(claim)
    assert fader.writes == []
    assert owner.declared_level_db() == -6.0


async def test_a_second_claim_of_one_kind_is_refused_not_stacked():
    fader = _Fader()
    owner = await _household(fader)
    await owner.acquire_level(ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB)

    with pytest.raises(VolumeClaimConflict):
        await owner.acquire_level(ClaimKind.SESSION_MEASUREMENT, -9.0)


@pytest.mark.parametrize(
    "kind,level",
    [
        (ClaimKind.TRANSIENT_DUCK, -10.0),
        (ClaimKind.HOUSEHOLD, -10.0),
        (ClaimKind.SESSION_MEASUREMENT, float("nan")),
        (ClaimKind.SESSION_MEASUREMENT, float("inf")),
        (ClaimKind.SESSION_MEASUREMENT, "loud"),
        # A bool is the one the shared field parser would take: it reads
        # ``True`` as ``1.0``, and a POSITIVE level is the loud direction.
        (ClaimKind.SESSION_MEASUREMENT, True),
    ],
)
async def test_a_level_the_owner_cannot_arbitrate_is_refused(kind, level):
    owner = await _household(_Fader())

    with pytest.raises(VolumeClaimRefused):
        await owner.acquire_level(kind, level)


# --- the release algebra (ADR-0004) -----------------------------------------


@pytest.mark.parametrize(
    "reference,current,depth,expected",
    [
        # A holder gives back its own attenuation and nothing else.
        (-20.0, -60.0, 40.0, -20.0),
        # ...but never ends above the level that should be in effect: a
        # volume change inside the window lowers the reference, and a bare
        # relative give-back would clamp back up to the old level.
        (-45.0, -60.0, 40.0, -45.0),
        # The relative bound wins when another holder is still down.
        (-20.0, -90.0, 40.0, -50.0),
        # An unreadable fader falls through to the reference.
        (-20.0, None, 40.0, -20.0),
    ],
)
def test_the_duck_release_gives_back_its_own_depth_and_no_more(
    reference, current, depth, expected,
):
    assert duck_release_target_db(
        reference_db=reference, current_db=current, depth_db=depth,
    ) == pytest.approx(expected)


async def test_ducks_stack_and_each_gives_back_only_its_own():
    fader = _Fader()
    owner = await _household(fader)

    first = await owner.acquire_duck(10.0)
    second = await owner.acquire_duck(40.0)
    assert fader.db == pytest.approx(HOUSEHOLD_DB - 50.0)

    await owner.release(second)
    assert fader.db == pytest.approx(HOUSEHOLD_DB - 10.0)

    await owner.release(first)
    assert fader.db == pytest.approx(HOUSEHOLD_DB)


async def test_a_duck_rides_the_claim_in_effect_not_the_household_level():
    """#2929's defect, made structural: the household level is not a bound."""
    fader = _Fader()
    owner = await _household(fader)
    await owner.acquire_level(ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB)

    duck = await owner.acquire_duck(40.0)
    assert fader.db == pytest.approx(MEASUREMENT_DB - 40.0)

    await owner.release(duck)
    assert fader.db == pytest.approx(MEASUREMENT_DB)


async def test_a_duck_over_no_declared_level_writes_nothing_and_still_releases():
    fader = _Fader()
    owner = _owner(fader)

    duck = await owner.acquire_duck(40.0)
    assert fader.writes == []

    await owner.release(duck)
    assert fader.writes == []


# --- MS-14: prove -----------------------------------------------------------


@pytest.mark.parametrize(
    "observed,proven",
    [
        (MEASUREMENT_DB, True),
        (MEASUREMENT_DB - 0.8 * READBACK_TOLERANCE_DB, True),
        (MEASUREMENT_DB - 2 * READBACK_TOLERANCE_DB, False),
        (MEASUREMENT_DB + 2 * READBACK_TOLERANCE_DB, False),
    ],
)
async def test_prove_answers_only_inside_the_one_confirm_tolerance(
    observed, proven,
):
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )
    fader.db = observed

    result = await owner.prove(claim)

    assert (result is not None) is proven
    if proven:
        assert result == pytest.approx(observed)


@pytest.mark.parametrize("failure", ["unreadable", "raises"])
async def test_an_unreadable_fader_is_never_proven(failure):
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )
    if failure == "unreadable":
        fader.readable = False
    else:
        fader.raise_on_read = True

    assert await owner.prove(claim) is None


async def test_a_ducked_fader_is_not_at_the_declared_level():
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )
    await owner.acquire_duck(40.0)

    assert await owner.prove(claim) is None


async def test_a_released_claim_proves_nothing():
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )
    await owner.release(claim)

    assert await owner.prove(claim) is None


async def test_the_positive_proof_line_says_the_proof_ran(caplog):
    """#2198: absence of a refusal must not read like a proof that never ran."""
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )

    with caplog.at_level(logging.INFO, logger="jasper.volume_owner"):
        assert await owner.prove(claim) is not None

    lines = [r.message for r in caplog.records if "volume.claim_proof" in r.message]
    assert lines and "result=held" in lines[-1]


@pytest.mark.parametrize(
    "readable,observed_is_empty", [(True, False), (False, True)],
)
async def test_an_empty_observed_db_means_the_fader_could_not_be_read(
    caplog, readable, observed_is_empty,
):
    """#2085: the one clean discriminator on a refusal line.

    It is a real observation on every line only because the proving read is
    unconditional — a short-circuit ahead of it would report "unreadable" for
    a claim that was merely preempted.
    """
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )
    await owner.acquire_level(ClaimKind.COMMISSIONING, -6.0)
    fader.readable = readable

    with caplog.at_level(logging.INFO, logger="jasper.volume_owner"):
        assert await owner.prove(claim) is None

    line = [r.message for r in caplog.records if "volume.claim_proof" in r.message][-1]
    assert "result=preempted" in line
    assert ('observed_db=""' in line) is observed_is_empty


# --- fail-closed writes -----------------------------------------------------


async def test_a_level_that_cannot_be_confirmed_leaves_no_claim_held():
    fader = _Fader()
    owner = await _household(fader)
    fader.accept = False

    with pytest.raises(VolumeClaimRefused):
        await owner.acquire_level(
            ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
        )

    assert owner.declared_level_db() == HOUSEHOLD_DB


async def test_a_household_declaration_that_cannot_be_confirmed_says_so():
    fader = _Fader()
    owner = _owner(fader)
    fader.accept = False

    assert await owner.declare_household_level_db(HOUSEHOLD_DB) is False
    assert owner.declared_level_db() == HOUSEHOLD_DB


@pytest.mark.parametrize("kind", [None, ClaimKind.SESSION_MEASUREMENT])
async def test_a_setter_that_reports_success_without_moving_is_not_believed(
    kind,
):
    """Every write confirms through an independent readback.

    This is what lets wave 5e delete the 1 Hz drift reconciler instead of
    replacing it: a write that did not land is an alarm at the write boundary,
    not something a cross-process patrol silently corrects a second later.
    ``set_volume_db`` returning ``True`` means the command was accepted, never
    that the fader moved.
    """
    fader = _Fader()
    owner = await _household(fader)
    fader.lies = True

    if kind is None:
        assert await owner.declare_household_level_db(-30.0) is False
        return
    with pytest.raises(VolumeClaimRefused):
        await owner.acquire_level(kind, MEASUREMENT_DB)
    assert owner.declared_level_db() == HOUSEHOLD_DB


async def test_the_owner_does_not_rewrite_a_level_the_fader_already_carries():
    """Arbitration re-derives the whole target; that must not become churn.

    CamillaDSP ramps every volume change over 400 ms, so a re-declared level
    that lands where the fader already sits has to cost nothing audible.
    """
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )
    await owner.acquire_duck(40.0)
    fader.writes.clear()

    await owner.declare_household_level_db(HOUSEHOLD_DB)

    assert fader.writes == []
    assert await owner.prove(claim) is None


async def test_a_release_that_lands_where_the_fader_sits_writes_nothing():
    """The other half of "arbitration is not churn": the RELEASE path.

    The household level moving inside the duck window is the shape that gets
    here — the voice duck re-declares it as part of its release, and the
    give-back then lands exactly where the duck already put the fader, so
    CamillaDSP is not asked to ramp 400 ms to where it already is.

    The skip is decided on the settle's OWN fresh read, never on the earlier
    give-back sample — see the foreign-write test below for what that
    distinction is worth.
    """
    fader = _Fader()
    owner = await _household(fader)
    duck = await owner.acquire_duck(10.0)
    assert fader.db == pytest.approx(HOUSEHOLD_DB - 10.0)
    fader.writes.clear()

    await owner.release(duck, household_level_db=HOUSEHOLD_DB - 10.0)

    assert fader.writes == []
    assert fader.db == pytest.approx(HOUSEHOLD_DB - 10.0)


async def test_a_foreign_write_landing_mid_release_is_repaired_not_skipped():
    """A release's two reads are NOT one question asked twice.

    They are separated by a round-trip, and the fader is shared across
    daemons. ``Ducker.restore`` clears the duck-active flag BEFORE awaiting
    ``release``, so jasper-control's probe
    (``control.volume_ops._make_duck_active_probe``) stops deferring and its
    own CamillaDSP write can land while the give-back read is in flight.

    The settle re-reads, sees the foreign value, and repairs. Deciding the
    skip on the earlier sample instead leaves the speaker wherever the
    foreign writer put it — here LOUDER than the level in effect, which is
    the direction that matters.
    """
    fader = _Fader()
    owner = await _household(fader)
    duck = await owner.acquire_duck(10.0)
    plain_get = fader.get
    landed = False

    async def racing_get():
        nonlocal landed
        answer = await plain_get()
        if not landed:
            # A foreign daemon writes between the sample and its return, so
            # `answer` is stale the moment the caller sees it.
            landed = True
            fader.db = -5.0
        return answer

    fader.get = racing_get
    try:
        await owner.release(duck, household_level_db=HOUSEHOLD_DB - 10.0)
    finally:
        fader.get = plain_get

    assert fader.writes != []
    assert fader.db == pytest.approx(HOUSEHOLD_DB - 10.0)


async def test_a_fader_that_drifted_off_the_level_is_repaired_not_skipped():
    """Skipping a redundant write must never mean skipping the check.

    This is the half that lets wave 5e delete the 1 Hz drift reconciler: the
    owner reads before it decides, so drift is repaired at the next claim
    boundary rather than patrolled for.
    """
    fader = _Fader()
    owner = await _household(fader)
    fader.db = HOUSEHOLD_DB - 9.0
    fader.writes.clear()

    await owner.declare_household_level_db(HOUSEHOLD_DB)

    assert fader.writes == [HOUSEHOLD_DB]
    assert fader.db == HOUSEHOLD_DB


async def test_a_refused_claim_hands_the_fader_back_instead_of_stranding_it():
    """The clamp shape: the write lands somewhere else and cannot confirm.

    The caller holds nothing after the raise, so its own release cannot put
    the fader back — the owner has to, or the speaker sits at a level no claim
    asked for.
    """
    fader = _Fader()
    owner = await _household(fader)
    # Below the measurement level, above the household one — so the refused
    # claim is the only thing the ceiling touches.
    fader.ceiling = -18.0

    with pytest.raises(VolumeClaimRefused):
        await owner.acquire_level(
            ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
        )

    assert fader.db == HOUSEHOLD_DB
    assert owner.declared_level_db() == HOUSEHOLD_DB


@pytest.mark.parametrize("taking", ["level", "duck"])
async def test_a_door_that_raises_leaves_no_claim_stranded_in_the_ledger(
    taking,
):
    """B1. A raise from the injected door must not leave a claim held.

    The ledger entry goes in before the fader is touched, so an escape between
    those two — the shape a door that raises instead of reporting produces —
    used to leave a claim nobody holds. Every later arbitration then answered
    against a level with no owner: the next duck would ride the dead claim's
    level, and its release would land there rather than on the household one.
    """
    fader = _Fader()
    owner = await _household(fader)
    fader.raise_on_write = True

    with pytest.raises(_DoorRaised):
        if taking == "level":
            await owner.acquire_level(
                ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
            )
        else:
            await owner.acquire_duck(40.0)

    assert owner.declared_level_db() == HOUSEHOLD_DB
    assert owner.duck_depth_db() == 0.0

    # ...and the ledger is not merely reported clean, it BEHAVES clean: a
    # later duck cycle rides the household level and gives it back.
    fader.raise_on_write = False
    fader.db = HOUSEHOLD_DB
    duck = await owner.acquire_duck(10.0)
    assert fader.db == pytest.approx(HOUSEHOLD_DB - 10.0)
    await owner.release(duck)
    assert fader.db == pytest.approx(HOUSEHOLD_DB)


async def test_prove_is_atomic_against_a_duck_landing_mid_read():
    """B2. The read and the verdict are one decision, under the owner's lock.

    The read is an await. Without the lock a duck acquired while it was in
    flight lands between the reading and the verdict, and ``prove`` then passes
    a PRE-duck number that agrees with the declared level while the speaker
    plays ducked — a proven level the speaker never had, which is the whole
    thing MS-14 refuses.

    The getter below captures its answer BEFORE yielding, so the reading is
    deliberately stale by the time the verdict runs. Under the lock the duck
    cannot land at all; without it, it does.
    """
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )
    raced: list[asyncio.Task] = []
    plain_get = fader.get

    async def racing_get():
        answer = await plain_get()
        if not raced:
            raced.append(asyncio.create_task(owner.acquire_duck(40.0)))
            # Run the duck as far as it can get. Under the lock it blocks on
            # the first await and these yields cost nothing; without the lock
            # it finishes, and `answer` is stale by the time it is judged.
            for _ in range(200):
                if raced[0].done():
                    break
                await asyncio.sleep(0)
        return answer

    fader.get = racing_get
    try:
        result = await owner.prove(claim)
        ducked_now = owner.duck_depth_db() > 0.0
    finally:
        fader.get = plain_get
        for task in raced:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert not (ducked_now and result is not None)


async def test_a_refused_household_level_on_release_leaves_the_claim_held():
    """A release that refuses must refuse having done NOTHING.

    ``household_level_db`` is validated inside the lock, and the ledger change
    is not undone if it fails. Deleting the claim first and validating second
    strands a ducked — quiet — speaker: the holder believes it released, its
    own restore has no claim left to give back, and the attenuation stands
    until some later arbitration happens to repair it.

    The claim survives intact, so the holder's ordinary release still works.
    """
    fader = _Fader()
    owner = await _household(fader)
    duck = await owner.acquire_duck(10.0)
    fader.writes.clear()

    with pytest.raises(VolumeClaimRefused):
        await owner.release(duck, household_level_db="loud")

    assert owner.holds(duck)
    assert fader.writes == []
    assert fader.db == pytest.approx(HOUSEHOLD_DB - 10.0)

    await owner.release(duck)
    assert fader.db == pytest.approx(HOUSEHOLD_DB)


async def test_release_is_idempotent_and_safe_against_nothing_held():
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )

    await owner.release(claim)
    fader.writes.clear()
    await owner.release(claim)

    assert fader.writes == []


async def test_every_write_the_owner_makes_goes_through_the_injected_door():
    """The clamp cannot be routed around: there is no second write path.

    ``_coerce_main_volume_db`` stays the defence-in-depth boundary and the
    owner is its only caller, so this asserts the owner opens no door of its
    own — every dB it lands is one the injected setter saw.
    """
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )
    duck = await owner.acquire_duck(40.0)
    await owner.release(duck)
    await owner.release(claim)

    assert fader.db == HOUSEHOLD_DB
    assert fader.writes == [
        HOUSEHOLD_DB,
        MEASUREMENT_DB,
        MEASUREMENT_DB - 40.0,
        MEASUREMENT_DB,
        HOUSEHOLD_DB,
    ]


# --- the process registration -----------------------------------------------


def test_no_owner_is_registered_until_a_process_installs_one():
    """``None`` is an honest answer, not a hole to plug.

    A caller that gets it is somewhere no owner was installed; minting one on
    the spot would make it the second, which is the arbitration failure this
    whole wave deletes wearing a new name.
    """
    assert volume_owner() is None


async def test_the_registration_replaces_rather_than_stacks():
    """One process, one owner — the same rule the household level follows."""
    # Also the leak check, and it is order-independent: whichever installing
    # test runs second sees the first one's owner if the autouse fixture ever
    # stops handing the process back the way it found it.
    assert volume_owner() is None
    first = _owner(_Fader())
    second = _owner(_Fader())

    install_volume_owner(first)
    assert volume_owner() is first
    install_volume_owner(second)
    assert volume_owner() is second

    install_volume_owner(None)
    assert volume_owner() is None


async def test_the_registered_owner_is_the_one_that_arbitrates():
    """Registration hands back a live owner, not a copy of one.

    The point of reaching for it at all is that a wizard's request handler and
    whatever else claims the fader in that process land on ONE ledger.
    """
    assert volume_owner() is None
    fader = _Fader()
    install_volume_owner(await _household(fader))

    reached = volume_owner()
    assert reached is not None
    claim = await reached.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
    )
    assert volume_owner() is not None
    assert volume_owner().declared_level_db() == MEASUREMENT_DB

    await volume_owner().release(claim)
    assert fader.db == pytest.approx(HOUSEHOLD_DB)


# --- relevel ----------------------------------------------------------------


async def test_relevel_moves_a_held_claim_in_one_write():
    """The floor-tone slider shape: the claim outlives the level it was taken at.

    Release-then-reacquire would step through the household level on the way,
    so the speaker would jump up and back down between two floors. One settle.
    """
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(ClaimKind.COMMISSIONING, -24.0)
    fader.writes.clear()

    moved = await owner.relevel(claim, -36.0)

    assert fader.writes == [-36.0]
    assert owner.declared_level_db() == -36.0
    assert owner.holds(moved) is True
    assert owner.holds(claim) is False


async def test_a_releveled_claim_still_releases_to_the_household_level():
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(ClaimKind.COMMISSIONING, -24.0)

    moved = await owner.relevel(claim, -36.0)
    await owner.release(moved)

    assert fader.db == pytest.approx(HOUSEHOLD_DB)


async def test_relevelling_a_claim_nobody_holds_is_refused():
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(ClaimKind.COMMISSIONING, -24.0)
    await owner.release(claim)

    with pytest.raises(VolumeClaimRefused):
        await owner.relevel(claim, -36.0)


async def test_a_relevel_that_cannot_be_established_keeps_the_claim():
    """FAIL-CLOSED HERE MEANS FAIL QUIET, and this is why it is not an unwind.

    A relevel's holder is mid-act with audio out, at a level QUIETER than the
    household one an unwind would hand back to. These very numbers say it:
    the claim sits at −24.0 and ``HOUSEHOLD_DB`` is −21.212124, so unwinding
    would raise the speaker 2.8 dB under a live tone. The claim therefore
    stays at the level it last CONFIRMED, and the caller — not a jump —
    decides abort or retry off the raise.
    """
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(ClaimKind.COMMISSIONING, -24.0)
    fader.ceiling = -30.0
    fader.writes.clear()

    with pytest.raises(VolumeClaimRefused):
        await owner.relevel(claim, -12.0)

    assert owner.declared_level_db() == -24.0
    assert owner.holds(claim) is True
    # The ORIGINAL handle survives, so the caller's own reference still works.
    await owner.release(claim)
    assert owner.declared_level_db() == HOUSEHOLD_DB


@pytest.mark.parametrize(
    "held_db, requested_db",
    [
        (-24.0, -12.0),   # asked louder, refused
        (-24.0, -36.0),   # asked quieter, refused
        (-40.0, -0.5),    # asked far louder, refused
        (-5.0, -60.0),    # asked far quieter, refused
    ],
)
async def test_an_unconfirmed_relevel_never_raises_the_effective_level(
    held_db, requested_db
):
    """THE PROPERTY, stated over the whole direction space rather than a case.

    Whatever was asked for and whichever way it pointed, a relevel that could
    not be confirmed must never leave the speaker louder than it already was.
    This is the pin that makes the unwind unreachable: restoring the old
    ``_unwind`` on the failure path turns the louder-request rows red, because
    the effective level would jump to the household claim underneath.
    """
    fader = _Fader()
    owner = await _household(fader)
    claim = await owner.acquire_level(ClaimKind.COMMISSIONING, held_db)
    before_db = fader.db
    before_declared = owner.declared_level_db()
    fader.ceiling = min(held_db, requested_db) - 1.0
    fader.writes.clear()

    with pytest.raises(VolumeClaimRefused):
        await owner.relevel(claim, requested_db)

    assert fader.db <= before_db
    assert owner.declared_level_db() <= before_declared
    assert owner.holds(claim) is True
