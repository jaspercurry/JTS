# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One owner arbitrates the main fader; 18 writers had nothing between them.

The subject is ``jasper.volume_owner`` — wave 5a of
``docs/REFACTOR-TUNING-2026-08.md`` §3. What is pinned here is the arbitration
a fader with 18 writers never had: which claim wins, what a release lands on,
and when a level counts as proven. The 0 dB ceiling is deliberately NOT pinned
here — it belongs to ``jasper.camilla._coerce_main_volume_db`` and its own
suite, and the property this file asserts instead is that the owner reaches the
fader only through the injected door, so that clamp cannot be routed around.
"""

from __future__ import annotations

import logging

import pytest

from jasper.active_speaker.volume_latch import READBACK_TOLERANCE_DB
from jasper.volume_owner import (
    ClaimKind,
    VolumeClaimConflict,
    VolumeClaimRefused,
    VolumeOwner,
    duck_release_target_db,
)

HOUSEHOLD_DB = -21.212124
MEASUREMENT_DB = -12.5


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

    async def set(self, db: float) -> bool:
        self.writes.append(float(db))
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
    return VolumeOwner(set_fader_db=fader.set, get_fader_db=fader.get)


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
