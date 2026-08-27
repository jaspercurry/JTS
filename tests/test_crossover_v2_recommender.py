# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ``Recommender`` seam filled: what the adapter owes over a real bundle.

``docs/REFACTOR-CUTOVER-2026-08.md`` §3's bar, and one property beyond it.

The bar: the real adapter, over a real bundle on disk, returns a mapping
carrying the packet's fingerprint and the staged section — **and leaves the
spool alone.** That last one is why the recommendation can be asked twice: a
``take`` slipping in where a ``pending`` belongs would hand the first caller the
prescription and tell the second there is none, and every other assertion here
would stay green while it did.

Beyond it: the round cross-check the adapter adds, which fails closed rather
than grading a proposal against the wrong round.

The bundle fixture is imported from the suite that owns that shape, the way
``test_crossover_v2_prescriber_status`` does, so a change to the real on-disk
tree reaches this suite too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.active_speaker.crossover_v2 import prescription_spool as spool
from jasper.cli.crossover_recommender import BankedRoundRecommender

from tests.test_crossover_v2_blend_prescription import _bundle

#: The round directory ``_bundle`` writes, and therefore the round every id a
#: session banked into it carries as its second path segment.
ROUND = "cap_TESTONLY"


@pytest.fixture(autouse=True)
def _isolated_spool(tmp_path: Path):
    """No test may see, or leave, a document in the real speaker's slot."""
    spool.set_prescription_spool_path_for_tests(tmp_path / "spool" / "pending.json")
    (tmp_path / "spool").mkdir()
    yield
    spool.set_prescription_spool_path_for_tests(None)


@pytest.fixture(autouse=True)
def _known_hostname(monkeypatch):
    """The report names a speaker, so no test reads the laptop's env."""
    monkeypatch.setenv("JASPER_HOSTNAME", "jts3.local")


def _stage_a_document() -> None:
    """Put a document in the pending slot.

    ``staged_prescription_pending`` is *"the stat, and only the stat"*, and
    ``take_staged_prescription`` consumes before it validates — so bytes are
    enough for both the fact this asserts and the mutation it must catch.
    """
    spool.prescription_spool_path().write_bytes(b"{}")


def _ids(*names: str) -> tuple[str, ...]:
    """Banked ids in the store's own shape: ``<root>/<relay>/<artifact>``."""
    return tuple(f"crossover_v2/{ROUND}/{name}" for name in names)


async def test_the_adapter_answers_with_the_packet_and_the_spool_untouched(tmp_path: Path):
    """§3's bar, whole."""
    session, state = _bundle(tmp_path)
    _stage_a_document()
    assert spool.staged_prescription_pending() is True

    answer = await BankedRoundRecommender(session, state_path=state)(
        _ids("positions/take-1.json", "candidate.json")
    )

    assert answer["packet"] is not None
    assert answer["packet_fingerprint"], "the packet's own identity is reported"
    assert answer["packet_fingerprint"] == answer["packet"]["packet_fingerprint"]
    assert answer["packet_error"] is None
    assert answer["staged"]["pending"] is True
    assert spool.staged_prescription_pending() is True, (
        "the recommendation consumed the staged prescription"
    )


async def test_a_second_ask_answers_the_same_because_nothing_was_consumed(tmp_path: Path):
    """The property the bar's last assertion exists to protect, stated directly."""
    session, state = _bundle(tmp_path)
    _stage_a_document()
    recommend = BankedRoundRecommender(session, state_path=state)

    first = await recommend(_ids("candidate.json"))
    second = await recommend(_ids("candidate.json"))

    assert first["staged"] == second["staged"]
    assert first["packet_fingerprint"] == second["packet_fingerprint"]


async def test_ids_from_another_round_refuse_rather_than_grade_the_wrong_one(tmp_path: Path):
    """``round_artifact_dir``'s two-rounds refusal, one step earlier.

    A packet built over whichever round the bundle happens to carry, for ids
    that named a different one, is a verdict about evidence nobody asked about.
    """
    session, state = _bundle(tmp_path)
    _stage_a_document()

    answer = await BankedRoundRecommender(session, state_path=state)(
        ("crossover_v2/cap_SOMEWHERE_ELSE/candidate.json",)
    )

    assert answer["packet"] is None
    assert "cap_SOMEWHERE_ELSE" in answer["packet_error"]
    assert ROUND in answer["packet_error"]
    # The spool lives on the speaker, not in the bundle, so it is still true.
    assert answer["staged"]["pending"] is True


async def test_ids_naming_two_rounds_refuse_without_picking_one(tmp_path: Path):
    session, state = _bundle(tmp_path)

    answer = await BankedRoundRecommender(session, state_path=state)(
        ("crossover_v2/cap_ONE/candidate.json", "crossover_v2/cap_TWO/check.json")
    )

    assert answer["packet"] is None
    assert "more than one round" in answer["packet_error"]


async def test_an_empty_walk_claims_no_round_and_still_reports(tmp_path: Path):
    """Anti-vacuity for the cross-check: it must not refuse what asserts nothing.

    ``TuningSession.recommend`` passes whatever was banked, and a session that
    banked nothing yet still has a question worth answering — the evidence is
    what a prescription would be produced FROM.
    """
    session, state = _bundle(tmp_path)

    answer = await BankedRoundRecommender(session, state_path=state)(())

    assert answer["packet"] is not None
    assert answer["packet_error"] is None
    assert answer["staged"]["pending"] is False
