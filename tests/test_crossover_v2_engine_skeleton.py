# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The engine skeleton's externally observable behaviour.

``docs/REFACTOR-TUNING-2026-08.md`` §3 wave 1. Three things can break here and
each gets one pin at one altitude:

1. **Ruling S12's surface is COMPLETE and LOUD.** Every mic-only regime the plan
   names is a parameter today, and invoking an unbuilt one returns a named
   disclosure rather than a silent skip.
2. **The three lifetimes open once and close once**, in the order MS-13 needs
   (the graph is proven before anything can play) and with the fader released
   even when the restore fails.
3. **MS-14's shape survives ruling S10**: an unproven level refuses to BANK the
   capture, never to play the stimulus.

Assertions are on types, codes and structured fields. The one place prose is
asserted is the S12 wording SHAPE, which the ruling fixes by quoting a
canonical sentence — there the sentence *is* the contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import pytest

from jasper.active_speaker.driver_acoustics import CAPTURE_GEOMETRIES
from jasper.audio_measurement.program_analysis import polarity_label

from jasper.active_speaker.crossover_v2.measure_spec import (
    DISTORTION_VS_LEVEL_NOT_IMPLEMENTED,
    INVERTED_POLARITY_NOT_IMPLEMENTED,
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
    MEASURE_KINDS,
    MEASURE_REGIMES,
    NEAR_FIELD_SPLICE_NOT_IMPLEMENTED,
    POLARITY_INVERTED,
    POLARITY_NORMAL,
    REGIME_NEAR_FIELD,
    REGIME_REFERENCE_AXIS,
    STUB_CODES,
    VERTICAL_AXIS_NOT_IMPLEMENTED,
    MeasureSpec,
    stubbed_capabilities,
)
from jasper.active_speaker.crossover_v2.playback_transaction import (
    PLAYBACK_STAGES,
    STAGE_ADMIT,
    STAGE_PLAY,
    STAGE_RESTORE,
    PlaybackOutcome,
)
from jasper.active_speaker.crossover_v2.session import (
    SessionStateError,
    TuningSession,
)
from jasper.active_speaker.crossover_v2.session_seams import (
    SESSION_SLOTS,
    EngineSeams,
)
from jasper.active_speaker.crossover_v2.spatial import POSITION_AXIS_VERTICAL


# --------------------------------------------------------------------------- #
# the smallest thing that satisfies the four seams
#
# NOT the wave-1 twin — that is PR 1b's, it is permanent infrastructure, and it
# has 21 importers to serve. This is three dozen lines of local double so these
# pins do not wait on it.
# --------------------------------------------------------------------------- #


@dataclass
class _Graph:
    fingerprint: str = "graph-abc"
    installs: int = 0
    restores: int = 0
    patches: list[Mapping[str, Any]] = field(default_factory=list)
    restore_raises: bool = False

    def install(self) -> str:
        self.installs += 1
        return self.fingerprint

    def patch(self, changes: Mapping[str, Any]) -> None:
        self.patches.append(changes)

    def restore(self) -> None:
        self.restores += 1
        if self.restore_raises:
            raise RuntimeError("restore blew up")


@dataclass
class _Volume:
    proven_db: float | None = -20.0
    acquired: list[float] = field(default_factory=list)
    releases: int = 0

    def acquire(self, level_db: float) -> None:
        self.acquired.append(level_db)

    def prove(self) -> float | None:
        return self.proven_db

    def release(self) -> None:
        self.releases += 1


@dataclass
class _Records:
    banked: list[Mapping[str, Any]] = field(default_factory=list)
    persisted: list[Mapping[str, Any]] = field(default_factory=list)

    def bank(self, record: Mapping[str, Any]) -> str:
        self.banked.append(record)
        return f"rec-{len(self.banked)}"

    def persist(self, state: Mapping[str, Any]) -> str:
        self.persisted.append(state)
        return f"state-{len(self.persisted)}"


@dataclass
class _Play:
    stage: str = STAGE_RESTORE
    incident: str = ""
    calls: list[tuple[MeasureSpec, int | None, float]] = field(default_factory=list)

    def run(
        self, *, spec: MeasureSpec, position_deg: int | None, level_db: float,
    ) -> PlaybackOutcome:
        self.calls.append((spec, position_deg, level_db))
        return PlaybackOutcome(stage_reached=self.stage, incident=self.incident)


@dataclass
class _Recommender:
    asked: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, record_ids: Sequence[str]) -> Mapping[str, Any]:
        self.asked.append(tuple(record_ids))
        return {"asked": len(record_ids)}


def _session(**overrides: Any) -> tuple[TuningSession, dict[str, Any]]:
    parts: dict[str, Any] = {
        "graph": _Graph(),
        "volume": _Volume(),
        "records": _Records(),
        "play": _Play(),
        "recommend": _Recommender(),
    }
    parts.update(overrides)
    session = TuningSession(
        session_id="s1",
        seams=EngineSeams(**parts),
        measurement_level_db=-20.0,
    )
    return session, parts


# --------------------------------------------------------------------------- #
# ruling S12 — the surface is complete, and every hole is named
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spec, code, instrument, captured",
    [
        (
            MeasureSpec(kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD),
            NEAR_FIELD_SPLICE_NOT_IMPLEMENTED, "R-3", True,
        ),
        (
            MeasureSpec(kind=MEASURE_KIND_BASELINE, polarity=POLARITY_INVERTED),
            INVERTED_POLARITY_NOT_IMPLEMENTED, "R-1", False,
        ),
        (
            MeasureSpec(
                kind=MEASURE_KIND_BASELINE, level_ladder_dbfs=(-20.0, -12.0),
            ),
            DISTORTION_VS_LEVEL_NOT_IMPLEMENTED, "R-4", True,
        ),
        (
            MeasureSpec(
                kind=MEASURE_KIND_BASELINE, position_axis=POSITION_AXIS_VERTICAL,
            ),
            VERTICAL_AXIS_NOT_IMPLEMENTED, "R-5a", False,
        ),
    ],
)
def test_every_unbuilt_mic_only_regime_is_a_named_stub(
    spec: MeasureSpec, code: str, instrument: str, captured: bool,
):
    """S12: the parameter exists today and says exactly what is missing."""
    stubs = stubbed_capabilities(spec)

    assert [stub.code for stub in stubs] == [code]
    assert stubs[0].instrument == instrument
    assert stubs[0].captured is captured
    assert code in STUB_CODES


def test_the_stub_sentence_renders_the_rulings_canonical_example():
    """The one wording pin. Ruling S12 quotes this sentence as the shape."""
    stub = stubbed_capabilities(
        MeasureSpec(kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD)
    )[0]

    assert stub.message == (
        "near-field splice not implemented; capture banked, splice pending R-3"
    )


def test_a_spec_asking_for_nothing_unbuilt_discloses_nothing():
    """Anti-vacuity: the stub check must be able to answer "no holes"."""
    assert stubbed_capabilities(MeasureSpec(kind=MEASURE_KIND_VERIFY)) == ()


def test_a_spec_may_trip_more_than_one_stub_at_once():
    stubs = stubbed_capabilities(MeasureSpec(
        kind=MEASURE_KIND_BASELINE,
        regime=REGIME_NEAR_FIELD,
        polarity=POLARITY_INVERTED,
    ))

    assert {stub.code for stub in stubs} == {
        NEAR_FIELD_SPLICE_NOT_IMPLEMENTED, INVERTED_POLARITY_NOT_IMPLEMENTED,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "measure"},
        {"kind": MEASURE_KIND_BASELINE, "regime": "far_field"},
        {"kind": MEASURE_KIND_BASELINE, "polarity": "flipped"},
        {"kind": MEASURE_KIND_BASELINE, "position_axis": "diagonal"},
        {
            "kind": MEASURE_KIND_BASELINE,
            "position_axis": POSITION_AXIS_VERTICAL,
            "positions": (15,),
        },
    ],
)
def test_a_spec_outside_the_vocabulary_is_refused_at_construction(kwargs: dict):
    """An out-of-vocabulary parameter fails at its own door.

    The last case is the one that is not a typo: a bearing on the vertical axis
    is a number nothing on this rig can command, and ``PositionGeometry`` makes
    the same refusal one layer down.
    """
    with pytest.raises(ValueError):
        MeasureSpec(**kwargs)


def test_the_measure_kinds_are_the_index_columns_and_no_more():
    """Wave 4j's ``kind`` column, and ruling S1's "measuring is measuring"."""
    assert MEASURE_KINDS == (
        MEASURE_KIND_BASELINE, MEASURE_KIND_CANDIDATE, MEASURE_KIND_VERIFY,
    )


def test_the_regime_handles_still_name_the_owning_modules_own_set():
    """Anti-drift: a handle that fell off ``CAPTURE_GEOMETRIES`` is a parameter
    nobody can pass, and the spec's own validator would reject it."""
    assert set(MEASURE_REGIMES) == set(CAPTURE_GEOMETRIES)
    assert {REGIME_NEAR_FIELD, REGIME_REFERENCE_AXIS} == set(MEASURE_REGIMES)


def test_the_polarity_words_are_the_measurement_frames_own():
    """``polarity_label`` calls itself "the ONE spelling of the map"."""
    assert POLARITY_NORMAL == polarity_label(1)
    assert POLARITY_INVERTED == polarity_label(-1)


# --------------------------------------------------------------------------- #
# the three lifetimes
# --------------------------------------------------------------------------- #


def test_the_session_owns_exactly_three_slots():
    """Three columns in the plan's diagram, and the plan adds no fourth."""
    assert len(SESSION_SLOTS) == 3
    assert len(set(SESSION_SLOTS)) == 3


def test_open_installs_the_graph_once_and_claims_the_declared_level():
    session, parts = _session()

    with session:
        assert parts["graph"].installs == 1
        assert parts["volume"].acquired == [-20.0]
        assert session.graph_fingerprint == "graph-abc"
        assert session.is_open

    assert parts["graph"].installs == 1
    assert parts["graph"].restores == 1
    assert parts["volume"].releases == 1
    assert not session.is_open


def test_opening_an_open_session_is_a_programming_error():
    session, _ = _session()

    with session:
        with pytest.raises(SessionStateError):
            session.open()


def test_closing_a_closed_session_is_not_an_error():
    session, parts = _session()
    session.open()
    session.close()
    session.close()

    assert parts["volume"].releases == 1


def test_an_open_that_cannot_claim_the_fader_puts_the_graph_back():
    """Half an open is a speaker measuring through a graph nobody holds."""

    class _RefusingVolume(_Volume):
        def acquire(self, level_db: float) -> None:
            raise RuntimeError("the household is holding it")

    session, parts = _session(volume=_RefusingVolume())

    with pytest.raises(RuntimeError):
        session.open()

    assert parts["graph"].restores == 1
    assert not session.is_open


def test_a_failing_graph_restore_still_gives_the_fader_back():
    """A session that died holding the claim leaves the speaker at a
    measurement level nobody chose."""
    session, parts = _session(graph=_Graph(restore_raises=True))
    session.open()

    with pytest.raises(RuntimeError):
        session.close()

    assert parts["volume"].releases == 1
    assert not session.is_open


def test_measure_refuses_a_session_that_was_never_opened():
    session, _ = _session()

    with pytest.raises(SessionStateError):
        session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))


# --------------------------------------------------------------------------- #
# the four verbs
# --------------------------------------------------------------------------- #


def test_one_measure_banks_one_record_per_position_it_names():
    session, parts = _session()

    with session:
        outcome = session.measure(MeasureSpec(
            kind=MEASURE_KIND_CANDIDATE, positions=(-22, 0, 22),
            candidate_id="cand-7",
        ))

    assert len(outcome.record_ids) == 3
    assert [call[1] for call in parts["play"].calls] == [-22, 0, 22]
    assert [row["position_deg"] for row in parts["records"].banked] == [-22, 0, 22]
    assert {row["kind"] for row in parts["records"].banked} == {
        MEASURE_KIND_CANDIDATE
    }
    assert {row["candidate_id"] for row in parts["records"].banked} == {"cand-7"}
    assert {row["graph_fingerprint"] for row in parts["records"].banked} == {
        "graph-abc"
    }


def test_a_spec_naming_no_position_still_measures_the_design_axis():
    session, parts = _session()

    with session:
        outcome = session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    assert len(outcome.record_ids) == 1
    assert parts["records"].banked[0]["position_deg"] is None


def test_an_unproven_level_refuses_to_bank_but_never_to_play():
    """MS-14 in the shape ruling S10 preserves.

    The stimulus still plays and the session can still measure again; what the
    unproven fader costs is the CLAIM, not the work.
    """
    session, parts = _session(volume=_Volume(proven_db=None))

    with session:
        outcome = session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    assert parts["play"].calls, "the stimulus must still have played"
    assert outcome.record_ids == ()
    assert parts["records"].banked == []
    assert outcome.playbacks[0].stage_reached == STAGE_RESTORE


def test_a_transaction_that_never_reached_play_banks_nothing():
    session, parts = _session(
        play=_Play(stage=STAGE_ADMIT, incident="relay_timeout")
    )

    with session:
        outcome = session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    assert outcome.record_ids == ()
    assert parts["records"].banked == []
    assert outcome.playbacks[0].incident == "relay_timeout"


def test_a_stub_that_captures_nothing_stops_the_stimulus():
    session, parts = _session()

    with session:
        outcome = session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, polarity=POLARITY_INVERTED,
        ))

    assert parts["play"].calls == []
    assert outcome.record_ids == ()
    assert [stub.code for stub in outcome.disclosures] == [
        INVERTED_POLARITY_NOT_IMPLEMENTED
    ]


def test_a_stub_whose_capture_still_happens_measures_and_discloses():
    """R-3: the near-field capture ships, and the splice is what is owed."""
    session, parts = _session()

    with session:
        outcome = session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD,
        ))

    assert len(outcome.record_ids) == 1
    assert parts["records"].banked[0]["regime"] == REGIME_NEAR_FIELD
    assert [stub.code for stub in outcome.disclosures] == [
        NEAR_FIELD_SPLICE_NOT_IMPLEMENTED
    ]


def test_analyze_reports_the_holes_and_needs_no_open_session():
    """Ruling S3: a banked session is re-analyzable offline, forever."""
    session, _ = _session()

    with session:
        session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD,
        ))

    outcome = session.analyze()

    assert not session.is_open
    assert [stub.code for stub in outcome.disclosures] == [
        NEAR_FIELD_SPLICE_NOT_IMPLEMENTED
    ]
    assert outcome.results == {}


def test_analyze_names_each_hole_once_however_many_specs_hit_it():
    """A ten-position near-field walk owes the splice once, not ten times."""
    session, _ = _session()

    with session:
        for _ in range(3):
            session.measure(MeasureSpec(
                kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD,
            ))

    assert [stub.code for stub in session.analyze().disclosures] == [
        NEAR_FIELD_SPLICE_NOT_IMPLEMENTED
    ]


def test_recommend_asks_the_prescriber_over_everything_banked():
    session, parts = _session()

    with session:
        session.measure(MeasureSpec(
            kind=MEASURE_KIND_CANDIDATE, positions=(0, 22),
        ))

    outcome = session.recommend()

    assert parts["recommend"].asked == [("rec-1", "rec-2")]
    assert outcome.record_ids == ("rec-1", "rec-2")
    assert outcome.recommendation == {"asked": 2}


def test_save_persists_the_session_state_over_the_records_measure_banked():
    session, parts = _session()

    with session:
        session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD,
        ))

    outcome = session.save()

    assert outcome.state_id == "state-1"
    assert outcome.record_ids == ("rec-1",)
    state = parts["records"].persisted[0]
    assert state["session_id"] == "s1"
    assert state["graph_fingerprint"] == "graph-abc"
    assert state["record_ids"] == ("rec-1",)
    assert state["disclosures"] == (NEAR_FIELD_SPLICE_NOT_IMPLEMENTED,)


# --------------------------------------------------------------------------- #
# the play transaction's own vocabulary
# --------------------------------------------------------------------------- #


def test_the_play_stages_are_the_five_the_ruling_names_in_order():
    assert PLAYBACK_STAGES == ("ready", "admit", "lock", "play", "restore")


@pytest.mark.parametrize(
    "stage, played",
    [("ready", False), ("admit", False), ("lock", False),
     ("play", True), ("restore", True)],
)
def test_a_transaction_played_only_once_it_reached_the_play_stage(
    stage: str, played: bool,
):
    assert PlaybackOutcome(stage_reached=stage).played is played


def test_an_outcome_naming_a_stage_that_does_not_exist_is_refused():
    with pytest.raises(ValueError):
        PlaybackOutcome(stage_reached="playing")


def test_a_clean_transaction_reaches_restore_not_play():
    """The restore runs on every path out, including the successful one."""
    assert PLAYBACK_STAGES[-1] == STAGE_RESTORE
    assert PLAYBACK_STAGES.index(STAGE_PLAY) < PLAYBACK_STAGES.index(STAGE_RESTORE)
