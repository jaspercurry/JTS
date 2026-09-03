# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The twin serves every shape of setup its 25 importers need.

The twin is budgeted *"as part of the engine, not as a follow-up"* (ruling S4,
ADR-0228), against proof it can carry the load:
``tests/crossover_v2_fixtures.py`` is 1,950 lines imported by 26 test files
totalling 57,079, and 25 of the 26 construct a session through it.

**Why this is a demonstration and not a port.** The honest proof would be one
of those 26 files rewritten against the twin with its assertions unchanged.
That cannot be done yet: those files assert on what a session DOES, and the new
engine's ``measure`` body lands in wave 2 — there is nothing yet for a ported
assertion to be true about. Faking it by porting a file and weakening its
assertions would prove the opposite of what it claims. So this walks the twin
through each USE CLASS the census found instead, and each test below names the
class it stands for and the importers in it.

The classes, and roughly how the 25 divide across them:

* **UC-1 — a whole session and a walk.** The bulk. Build it, measure, assert on
  what was banked.
* **UC-2 — one seam misbehaving.** A failure injected at one place, the rest of
  the session real.
* **UC-3 — a seam swapped mid-test**, or a second session built from the first's
  parts.
* **UC-4 — declarations only.** No session at all; the file wants the numbers.
* **UC-5 — read the bank back.** What ``analyze`` will do offline (ruling S3).
* **UC-6 — behaviour that differs by measurement KIND.** Where the old
  fixture's swappable check/measure/verify capture factories land, now that
  ruling S1 has made those one verb with a ``kind`` argument.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
)
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.crossover_v2.playback_transaction import (
    STAGE_ADMIT,
    STAGE_RESTORE,
)
from jasper.active_speaker.crossover_v2.session import (
    UNPROVEN_LEVEL,
    SessionStateError,
)

from tests import engine_declarations as decl
from tests.engine_twin import (
    FakeGraph,
    FakePlay,
    FakeRecords,
    FakeSeams,
    FakeVolume,
    GraphInstallFailed,
    SeamFailure,
    tuning_session,
    open_session,
)


def _walk(kind: str = MEASURE_KIND_BASELINE, **kwargs) -> MeasureSpec:
    """The fixture speaker's horizontal walk, as a spec."""
    return MeasureSpec(
        kind=kind,
        positions=decl.WALK_DEG,
        pose_prompts=decl.WALK_PROMPTS,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# UC-1 — a whole session and a walk
#
# The class the old fixture's `_conductor` + `_run_phase` pair served, and the
# one most of the 25 are in: build a session, walk it, assert on the bank.
# --------------------------------------------------------------------------- #


async def test_uc1_a_default_twin_walks_a_session_end_to_end():
    async with open_session() as (session, fakes):
        outcome = await session.measure(_walk())

    assert fakes.play.bearings == list(decl.WALK_DEG)
    assert len(outcome.record_ids) == len(decl.WALK_DEG)
    assert [r["position_deg"] for r in fakes.banked] == list(decl.WALK_DEG)
    assert [r["prompt"] for r in fakes.banked] == list(decl.WALK_PROMPTS)
    assert {r["level_db"] for r in fakes.banked} == {decl.SESSION_VOLUME_DB}


async def test_uc1_the_twin_drives_the_measure_verb():
    """The measure verb alone: a harness that could only reach ``measure``
    is exactly the twin's surface until later waves land the rest."""
    async with open_session() as (session, _):
        measured = await session.measure(_walk(MEASURE_KIND_CANDIDATE))

    assert measured.record_ids != ()


async def test_uc1_a_ladder_is_position_times_rung():
    async with open_session() as (session, fakes):
        outcome = await session.measure(_walk(level_ladder_dbfs=decl.LADDER_DBFS))

    expected = len(decl.WALK_DEG) * len(decl.LADDER_DBFS)
    assert len(outcome.stimuli) == expected
    assert fakes.play.rungs == list(decl.LADDER_DBFS) * len(decl.WALK_DEG)
    # One claim, taken once: a ladder moves the stimulus, never the fader.
    assert fakes.volume.acquired == [decl.SESSION_VOLUME_DB]


async def test_uc1_the_session_is_handed_back_closed_so_the_test_owns_the_lifetime():
    """``tuning_session`` is the un-opened door; half these tests are ABOUT
    opening, so the harness must not have done it already."""
    session, fakes = tuning_session()

    assert not session.is_open
    assert fakes.graph.installs == 0
    with pytest.raises(SessionStateError):
        await session.measure(_walk())


# --------------------------------------------------------------------------- #
# UC-2 — one seam misbehaving
# --------------------------------------------------------------------------- #


async def test_uc2_a_fader_that_cannot_be_proven_refuses_only_its_own_stimulus():
    """MS-14 in ruling S10's shape, expressed as a sequence of readings —
    because the fact that matters is a claim preempted MID-WALK."""
    volume = FakeVolume(readings=[decl.SESSION_VOLUME_DB, None])
    async with open_session(FakeSeams(volume=volume)) as (session, fakes):
        outcome = await session.measure(_walk())

    assert volume.proves == len(decl.WALK_DEG)
    assert [s.banked for s in outcome.stimuli] == [True, False, True]
    assert outcome.stimuli[1].incident == UNPROVEN_LEVEL
    assert len(fakes.banked) == 2, "the walk went on past the unproven rung"


async def test_uc2_a_transaction_that_never_played_banks_nothing_and_says_why():
    play = FakePlay(script=[
        (STAGE_RESTORE, ""),
        (STAGE_ADMIT, "capture_timeout"),
        (STAGE_RESTORE, ""),
    ])
    async with open_session(FakeSeams(play=play)) as (session, fakes):
        outcome = await session.measure(_walk())

    assert [s.incident for s in outcome.stimuli] == ["", "capture_timeout", ""]
    assert len(fakes.banked) == 2


async def test_uc2_an_install_that_fails_leaves_nothing_held():
    session, fakes = tuning_session(FakeSeams(graph=FakeGraph(install_raises=True)))

    with pytest.raises(GraphInstallFailed):
        await session.open()

    assert fakes.graph.restores == 1
    assert not fakes.volume.held
    assert not session.is_open


async def test_uc2_a_release_that_fails_is_retried_by_the_next_close():
    volume = FakeVolume(release_raises=True)
    session, _ = tuning_session(FakeSeams(volume=volume))
    await session.open()

    with pytest.raises(SeamFailure):
        await session.close()
    volume.release_raises = False
    await session.close()

    assert volume.releases == 2
    assert not session.is_open


# --------------------------------------------------------------------------- #
# UC-3 — a seam swapped, or a second session built from the first's parts
# --------------------------------------------------------------------------- #


def test_uc3_replace_swaps_one_seam_and_leaves_the_rest_alone():
    base = FakeSeams()
    strict = base.replace(volume=FakeVolume(proven_db=None))

    assert strict.volume is not base.volume
    assert strict.graph is base.graph
    assert strict.records is base.records


def test_uc3_dataclasses_replace_works_on_the_bundle_too():
    """Both spellings, because a test reads better with whichever it reaches
    for and a harness should not make that a decision."""
    base = FakeSeams()
    swapped = replace(base, play=FakePlay(default=(STAGE_ADMIT, "capture_timeout")))

    assert swapped.play is not base.play
    assert swapped.volume is base.volume


async def test_uc3_two_sessions_can_share_one_record_store():
    """A store outliving a session is what a campaign's bank IS, so the twin
    must not tie one to the other."""
    shared = FakeRecords()

    async with open_session(FakeSeams(records=shared)) as (first, _):
        await first.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    async with open_session(
        FakeSeams(records=shared), session_id="twin-session-2",
    ) as (second, _):
        await second.measure(MeasureSpec(kind=MEASURE_KIND_CANDIDATE))

    assert shared.kinds() == [MEASURE_KIND_BASELINE, MEASURE_KIND_CANDIDATE]
    assert {r["session_id"] for r in shared.banked} == {
        decl.SESSION_ID, "twin-session-2",
    }


def test_uc3_a_declaration_the_signature_does_not_name_is_one_keyword_away():
    """The ``**kwargs`` passthrough. A harness that had to grow a parameter per
    engine field would be behind the engine by its second wave."""
    session, _ = tuning_session(
        session_id="explicit", measurement_level_db=-30.0,
    )

    assert session.session_id == "explicit"
    assert session.measurement_level_db == -30.0


# --------------------------------------------------------------------------- #
# UC-4 — declarations only, no session
# --------------------------------------------------------------------------- #


def test_uc4_the_declarations_module_imports_nothing():
    """The cheap module stays cheap. A test that wants the corner should not
    pay the engine's import cost — the lesson wave 1a's review learned about
    ``measure_spec``'s vocabulary, applied to the test surface."""
    import ast
    from pathlib import Path

    source = Path(decl.__file__).read_text(encoding="utf-8")
    imported = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
    ]

    assert imported == []


def test_uc4_the_declarations_are_internally_consistent():
    """Not a claim about any real driver — a claim that the fixture speaker is
    not self-contradicting, which is the only thing a fixture owes."""
    woofer_lo, woofer_hi = decl.ROLE_BANDS_HZ[decl.ROLE_WOOFER]
    tweeter_lo, tweeter_hi = decl.ROLE_BANDS_HZ[decl.ROLE_TWEETER]

    assert tweeter_lo < decl.FC_HZ < woofer_hi, "the roles overlap at the corner"
    assert woofer_lo < decl.FC_HZ < tweeter_hi
    assert decl.SESSION_VOLUME_DB < 0.0, "the hearing clamp is never relaxed"
    assert len(decl.WALK_PROMPTS) == len(decl.WALK_DEG)
    assert set(decl.DRIVER_CAPS_DBFS) == set(decl.ROLES)


# --------------------------------------------------------------------------- #
# UC-5 — the bank outlives its session
# --------------------------------------------------------------------------- #


async def test_uc5_the_bank_outlives_the_session_that_wrote_it():
    fakes = FakeSeams()
    async with open_session(fakes) as (session, _):
        outcome = await session.measure(_walk())

    assert not session.is_open
    assert len(fakes.records.banked) == len(outcome.record_ids)
    assert len(fakes.records.by_position(0)) == 1


# --------------------------------------------------------------------------- #
# UC-6 — behaviour that differs by measurement KIND
#
# The old fixture's swappable check/measure/verify capture factories, in the
# vocabulary ruling S1 left: one verb, three arguments.
# --------------------------------------------------------------------------- #


async def test_uc6_playback_can_differ_by_kind():
    play = FakePlay(by_kind={MEASURE_KIND_VERIFY: (STAGE_ADMIT, "capture_timeout")})
    fakes = FakeSeams(play=play)

    async with open_session(fakes) as (session, _):
        baseline = await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))
        verify = await session.measure(MeasureSpec(kind=MEASURE_KIND_VERIFY))

    assert baseline.record_ids != ()
    assert verify.record_ids == ()
    assert verify.stimuli[0].incident == "capture_timeout"


@pytest.mark.parametrize(
    "kind", [MEASURE_KIND_BASELINE, MEASURE_KIND_CANDIDATE, MEASURE_KIND_VERIFY],
)
async def test_uc6_every_kind_is_the_same_verb_through_the_twin(kind: str):
    """Ruling S1's *"measuring is measuring"*, checked at the harness: the
    three differ by an argument and by nothing in the setup."""
    async with open_session() as (session, fakes):
        outcome = await session.measure(MeasureSpec(kind=kind))

    assert len(outcome.record_ids) == 1
    assert fakes.records.kinds() == [kind]


# --------------------------------------------------------------------------- #
# the twin's own boundaries
# --------------------------------------------------------------------------- #


def test_the_twin_imports_no_other_test_module_but_its_declarations():
    """The edge the old fixture has and this one must not inherit.

    ``crossover_v2_fixtures`` imports its preset from
    ``tests/test_active_speaker_profile.py`` — a 1,950-line fixture library
    depending on a 493-line TEST file that 39 other files import. The twin owns
    its declarations instead.
    """
    import ast
    from pathlib import Path

    from tests import engine_twin

    source = Path(engine_twin.__file__).read_text(encoding="utf-8")
    reached = {
        node.module for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tests")
    }

    assert reached == {"tests.engine_declarations"}


def test_the_twin_does_not_import_the_fixture_it_replaces():
    """A twin that reached back into the thing it replaces would keep it alive
    rather than retire it. The old fixture is NOT deleted here — its importers
    migrate in waves 2 and 3, and it dies with its last one.

    An AST walk, not a grep: both modules NAME the fixture and the flow in
    prose, because saying what a module replaces is the docstring's job. What
    must not exist is an import of either.
    """
    import ast
    from pathlib import Path

    from tests import engine_twin

    forbidden = ("crossover_v2_fixtures", "crossover_v2_flow")
    for module in (engine_twin, decl):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        reached: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                reached.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                reached.add(node.module or "")
                reached.update(
                    f"{node.module}.{alias.name}" for alias in node.names
                )
        offenders = [
            name for name in reached
            if any(bad in name for bad in forbidden)
        ]
        assert not offenders, (module.__name__, offenders)
