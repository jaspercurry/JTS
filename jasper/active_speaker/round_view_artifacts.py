# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Round artifact names and producer metadata shared by completion and the CLI."""
from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple
from jasper.audio_measurement.evidence_reasons import (
    REASON_REFUSED as REASON_REFUSED,
    REASON_UNREADABLE as REASON_UNREADABLE,
    REASON_UNWRITABLE as REASON_UNWRITABLE,
)
from .run_manifest import RUN_MANIFEST_FILENAME
from .bench.replay import DSP_LEVELS_SCHEMA, DSP_REPLAY_SCHEMA
from .measurement_bass import BASS_VIEW_SCHEMA
from .measurement_programs import PURPOSE_BASS, PURPOSE_REAR, PURPOSE_REFERENCE, PURPOSE_ROOM, PURPOSE_SPEAKER
from .frequency_view import FREQUENCY_VIEW_FILENAME, SCHEMA as FREQUENCY_VIEW_SCHEMA
from .crossover_v2.evidence_packet.offline_reads import CLASSIFICATION_ARTIFACT, HARMONICS_ARTIFACT
from .crossover_v2.position_cycle import POSITION_CYCLE_FILENAME
from .crossover_v2.round_inputs import ROOM_ARTIFACT, RoundInputs, banked_round_of, recent_round_sessions

TAKES_THIS_ROUND = "<this-round>"
TAKES_THIS_BUNDLE = "<this-round's bundle>"
TAKES_SET = (TAKES_THIS_ROUND, "--set", "<set-id>")
TAKES_BEFORE_ANOTHER = (TAKES_THIS_ROUND, "<other-round>")
TAKES_FAR_AND_CLOSE = (
    "--far-round", TAKES_THIS_ROUND, "--close-round", "<other-round>",
    "--close-m", "<distance-m>",
)


class ViewArtifact(NamedTuple):
    """One artifact, the command that makes it, and where it lands.

    ``in_artifact_dir`` marks an artifact the round's own evidence holds; every
    view files beside the round instead, never inside its evidence.
    ``producer`` overrides the command; otherwise the key names the subcommand.
    ``bookkeeping`` names the purposes whose round publishes this view by
    itself, in :data:`BOOKKEEPING_ORDER`, through ``builder`` — this package's
    ``<module>.<function>`` answering ``(payload, provenance fields)``;
    ``packet`` is the analysis family carrying it in ``packet.json``.
    ``schema`` names the artifact's shape and is the ``schema`` the answer of
    the view that writes it carries; empty for an artifact no view writes.
    ``per_take`` marks a view that files one artifact per take it reads
    (:func:`~.crossover_v2.round_inputs.take_artifact_name`), so no round
    inventory row stands for it.
    """

    artifact: str
    takes: tuple[str, ...] = (TAKES_THIS_ROUND,)
    in_artifact_dir: bool = False
    producer: str | None = None
    purposes: tuple[str, ...] = ()
    bookkeeping: tuple[str, ...] = ()
    grades_against_base: bool = False
    builder: str | None = None
    packet: str | None = None
    schema: str = ""
    per_take: bool = False

    @property
    def per_set(self) -> bool: return "<set-id>" in self.takes

ARTIFACT_BY_VIEW: dict[str, ViewArtifact] = {
    "inventory": ViewArtifact("inventory.json", TAKES_SET, bookkeeping=(PURPOSE_SPEAKER, PURPOSE_ROOM, PURPOSE_BASS, PURPOSE_REAR), builder="round_bookkeeping.inventory", schema="jts_inventory/1"),
    "run-manifest": ViewArtifact(RUN_MANIFEST_FILENAME, in_artifact_dir=True, producer="plan_run.run_plan"),
    "dsp-replay": ViewArtifact("dsp_replay.json", ("<graph.yml>", "<stimulus.wav>", "--main-db", "<db>", "--out", "<render-dir>"), schema=DSP_REPLAY_SCHEMA),
    "dsp-levels": ViewArtifact("dsp_levels.json", ("<dsp_replay.json>", "--raw", "<output.f64le>", "--window-s", "<start>", "<stop>"), schema=DSP_LEVELS_SCHEMA),
    "bass-fit-table": ViewArtifact("bass_table.json", (TAKES_THIS_ROUND, "--candidate", "<candidate.json>"), purposes=(PURPOSE_BASS,), packet="bass", schema="jts_bass_run_table/1"),
    "entry": ViewArtifact("entry_state_grade.json", purposes=(PURPOSE_SPEAKER,), schema="jts_entry_state_grade/1"),
    "repeat": ViewArtifact("repeatability.json", TAKES_BEFORE_ANOTHER, schema="jts_repeatability/1"),
    "candidates": ViewArtifact("candidates.json", schema="jts_candidates/2"),
    "directivity": ViewArtifact("directivity.json", TAKES_SET, purposes=(PURPOSE_SPEAKER,), schema="jts_directivity/1"),
    "sweep --scope round": ViewArtifact("gate_sweep.json", TAKES_SET, schema="jts_gate_sweep/1"),
    "sweep --scope take": ViewArtifact("window_view.json", (*TAKES_SET, "--take", "<take-id>"), schema=FREQUENCY_VIEW_SCHEMA),
    "impulse": ViewArtifact("impulse.json", (*TAKES_SET, "--take", "<take-id>"), schema="jts_impulse/1", per_take=True),
    "group-delay": ViewArtifact("group_delay.json", (*TAKES_SET, "--take", "<take-id>"), schema="jts_group_delay/1",
                                per_take=True),
    "decay": ViewArtifact("decay.json", (*TAKES_SET, "--take", "<take-id>"), schema="jts_decay/1", per_take=True),
    "compare": ViewArtifact("compare.json", (
        "<round-a>", TAKES_THIS_ROUND, "--a-take", "<take-id>", "--b-take", "<take-id>",
    ), schema="jts_compare/1", per_take=True),
    "frequency": ViewArtifact(FREQUENCY_VIEW_FILENAME, bookkeeping=(PURPOSE_ROOM, PURPOSE_BASS, PURPOSE_REAR), builder="round_bookkeeping.frequency", schema=FREQUENCY_VIEW_SCHEMA),
    # The batch spans one set per played candidate, so this view reads the
    # round rather than a set.
    "rear": ViewArtifact(
        "rear_view.json", purposes=(PURPOSE_REAR,),
        bookkeeping=(PURPOSE_REAR,), builder="round_view_builders.rear", packet="rear", schema="jts_rear_view/1",
    ),
    "bass": ViewArtifact("bass_view.json", TAKES_SET, purposes=(PURPOSE_BASS,), bookkeeping=(PURPOSE_BASS,), builder="round_bookkeeping.bass", packet="bass", schema=BASS_VIEW_SCHEMA),
    "bass-compare": ViewArtifact("bass_comparison.json", (
        "<before-round>", TAKES_THIS_ROUND, "--before-set", "<before-set-id>",
        "--after-set", "<set-id>", "--change", "<change>",
    ), purposes=(PURPOSE_BASS,), packet="bass", schema="jts_bass_comparison/1"),
    "delay-landscape": ViewArtifact("delay_landscape.json", purposes=(PURPOSE_SPEAKER,), schema="jts_delay_landscape/1"),
    "close-reference": ViewArtifact("close_reference.json", TAKES_FAR_AND_CLOSE, purposes=(PURPOSE_SPEAKER,), schema="jts_close_reference/1"),
    "nearfield": ViewArtifact("nearfield_view.json", purposes=(PURPOSE_REFERENCE,), schema="jts_nearfield_view/1"),
    "room": ViewArtifact(ROOM_ARTIFACT, TAKES_SET, purposes=(PURPOSE_ROOM,), bookkeeping=(PURPOSE_ROOM,), builder="round_bookkeeping.room", packet="room", schema="jts_room/1"),
    # The packet owns these two names, so the rows take those constants rather
    # than a second spelling of them.
    "distortion": ViewArtifact(HARMONICS_ARTIFACT, purposes=(PURPOSE_SPEAKER,), schema="jts_harmonic_distortion/1"),
    "classify-features": ViewArtifact(
        CLASSIFICATION_ARTIFACT, purposes=(PURPOSE_SPEAKER,), schema="jts_feature_classification/1",
    ),
    "room-grade": ViewArtifact("room_grade.json", TAKES_SET, purposes=(PURPOSE_ROOM,), bookkeeping=(PURPOSE_ROOM,), grades_against_base=True, builder="round_bookkeeping.room_grade", packet="room", schema="jts_room_grade/1"),
    # The banker writes this index; inventory reports its presence.
    "position-cycle": ViewArtifact(
        POSITION_CYCLE_FILENAME, ("--run", "<run-id>"), producer="jasper-round wait",
    ),
}

#: The run order of the views a finished round publishes: ``room-grade``
#: grades the median ``room`` wrote, so this is a data dependency.
BOOKKEEPING_ORDER = ("room", "room-grade", "bass", "rear", "frequency", "inventory")
#: The analysis families, in the order ``packet.json`` carries their keys.
PACKET_FAMILIES = tuple(dict.fromkeys(row.packet for row in map(ARTIFACT_BY_VIEW.__getitem__, BOOKKEEPING_ORDER) if row.packet))

VIEW_PURPOSES = {
    **{name.split()[0]: spec.purposes for name, spec in ARTIFACT_BY_VIEW.items()},
    "speaker-fit": (PURPOSE_SPEAKER,),
}

#: The answers whose shape no row above names: they write no artifact, or
#: one no inventory lists.
ANSWER_SCHEMAS = {
    "speaker-fit": "jts_speaker_fit/1",
    "repeat --set": "jts_repeat/1",
    "close-reference --distance": "jts_mic_distance/1",
}

INVENTORY_ARTIFACT = ARTIFACT_BY_VIEW["inventory"].artifact


def context_artifacts(inputs: RoundInputs, round_dir: Path) -> dict[str, Any]:
    """Paths and sizes only; optional agent prose never becomes measurement data."""
    bundles = recent_round_sessions(inputs.session_dir)
    latest_note = next((
        path
        for bundle in bundles
        for path in dict.fromkeys((
            (banked_round_of(bundle) or bundle) / "agent_notes.md",
            bundle / "agent_notes.md",
        ))
        if path.is_file()
    ), None)
    return {
        key: {
            "path": str(path) if path else None,
            "present": path is not None and path.is_file(),
            "bytes": path.stat().st_size if path and path.is_file() else None,
        }
        for key, path in (("latest_agent_note", latest_note),)
    }


PROG = "jasper-round-views"


def bookkeeping_views(purpose: str, *, has_room: bool = False,
                      co_purposes: tuple[str, ...] = ()) -> tuple[tuple[str, bool, bool], ...]:
    """View name, per-set scope, and whether it grades against the base."""
    wanted = {purpose, *co_purposes} | ({PURPOSE_ROOM} if purpose == PURPOSE_SPEAKER and has_room else set())
    rows = ((name, ARTIFACT_BY_VIEW[name]) for name in BOOKKEEPING_ORDER)
    # A speaker round takes its frequency view from the packet writer, not here.
    return tuple((name, row.per_set, row.grades_against_base) for name, row in rows
                 if wanted.intersection(row.bookkeeping) and (purpose != PURPOSE_SPEAKER or name != "frequency"))
