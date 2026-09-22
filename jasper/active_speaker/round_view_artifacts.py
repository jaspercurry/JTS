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
from .measurement_programs import PURPOSE_BASS, PURPOSE_REAR, PURPOSE_ROOM, PURPOSE_SPEAKER
from .frequency_view import FREQUENCY_VIEW_FILENAME
from .crossover_v2.evidence_packet.offline_reads import CLASSIFICATION_ARTIFACT, HARMONICS_ARTIFACT
from .crossover_v2.position_cycle import POSITION_CYCLE_FILENAME
from .crossover_v2.round_inputs import ROOM_ARTIFACT, RoundInputs, banked_round_of, recent_round_sessions

TAKES_THIS_ROUND = "<this-round>"
TAKES_THIS_BUNDLE = "<this-round's bundle>"
TAKES_SET = (TAKES_THIS_ROUND, "--set", "<set-id>")
TAKES_AFTER_ANOTHER = ("<other-round>", TAKES_THIS_ROUND)
TAKES_BEFORE_ANOTHER = (TAKES_THIS_ROUND, "<other-round>")
TAKES_FAR_AND_CLOSE = (
    "--far-round", TAKES_THIS_ROUND, "--close-round", "<other-round>",
    "--close-m", "<distance-m>",
)


class ViewArtifact(NamedTuple):
    """One artifact, the command that makes it, and where it lands.

    ``in_artifact_dir`` marks the views the evidence PACKET reads: those file
    into the round's own artifact directory, the only path that reader looks
    at, rather than beside the round where an operator reads the rest.
    ``producer`` overrides the command; otherwise the key names the subcommand.
    ``bookkeeping`` names the purposes whose round publishes this view by
    itself, in :data:`BOOKKEEPING_ORDER`, through ``builder`` — this package's
    ``<module>.<function>`` answering ``(payload, provenance fields)``;
    ``packet`` is the analysis family carrying it in ``packet.json``.
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

    @property
    def per_set(self) -> bool: return "<set-id>" in self.takes

#: ``repeat-floor`` publishes to ``--install`` or ``--out``, not beside the round.
ARTIFACT_BY_VIEW: dict[str, ViewArtifact] = {
    "inventory": ViewArtifact("inventory.json", TAKES_SET, bookkeeping=(PURPOSE_SPEAKER, PURPOSE_ROOM, PURPOSE_BASS, PURPOSE_REAR), builder="round_bookkeeping.inventory"),
    "run-manifest": ViewArtifact(RUN_MANIFEST_FILENAME, in_artifact_dir=True, producer="plan_run.run_plan"),
    "dsp-replay": ViewArtifact("dsp_replay.json", ("<graph.yml>", "<stimulus.wav>", "--main-db", "<db>", "--bass-reference-db", "<db>", "--out", "<render-dir>")),
    "dsp-levels": ViewArtifact("dsp_levels.json", ("<dsp_replay.json>", "--raw", "<output.f64le>", "--window-s", "<start>", "<stop>")),
    "bass-fit-table": ViewArtifact("bass_table.json", (TAKES_THIS_ROUND, "--candidate", "<candidate.json>"), purposes=(PURPOSE_BASS,), packet="bass"),
    "entry": ViewArtifact("entry_state_grade.json", purposes=(PURPOSE_SPEAKER,)),
    "frozen": ViewArtifact("frozen_reference.json", TAKES_AFTER_ANOTHER, purposes=(PURPOSE_SPEAKER,)),
    "per-seat": ViewArtifact("per_seat.json", purposes=(PURPOSE_ROOM, PURPOSE_SPEAKER)),
    "repeat": ViewArtifact("repeatability.json", TAKES_BEFORE_ANOTHER),
    "candidates": ViewArtifact("candidates.json"),
    "agreement": ViewArtifact("agreement.json", (TAKES_THIS_ROUND, "--include", "agreement"), producer="jasper-round-views per-seat", purposes=(PURPOSE_ROOM, PURPOSE_SPEAKER)),
    "co-metrics": ViewArtifact("audibility_co_metrics.json", (TAKES_THIS_ROUND, "--include", "co-metrics"), producer="jasper-round-views per-seat", purposes=(PURPOSE_ROOM, PURPOSE_SPEAKER)),
    "directivity": ViewArtifact("directivity.json", (TAKES_THIS_ROUND, "--include", "directivity"), producer="jasper-round-views per-seat", purposes=(PURPOSE_ROOM, PURPOSE_SPEAKER)),
    "cloud-binding": ViewArtifact("cloud_binding.json", purposes=(PURPOSE_SPEAKER,)),
    "sweep --scope verdict": ViewArtifact("spec_gate_sensitivity.json", TAKES_SET),
    "sweep --scope round": ViewArtifact("gate_sweep.json", TAKES_SET),
    "sweep --scope take": ViewArtifact("window_view.json", (*TAKES_SET, "--take", "<take-id>")),
    "frequency": ViewArtifact(FREQUENCY_VIEW_FILENAME, bookkeeping=(PURPOSE_ROOM, PURPOSE_BASS, PURPOSE_REAR), builder="round_bookkeeping.frequency"),
    # The batch spans one set per played candidate, so this view reads the
    # round rather than a set.
    "rear": ViewArtifact(
        "rear_view.json", purposes=(PURPOSE_REAR,),
        bookkeeping=(PURPOSE_REAR,), builder="round_view_builders.rear", packet="rear",
    ),
    "bass": ViewArtifact("bass_view.json", TAKES_SET, purposes=(PURPOSE_BASS,), bookkeeping=(PURPOSE_BASS,), builder="round_bookkeeping.bass", packet="bass"),
    "bass-compare": ViewArtifact("bass_comparison.json", (
        "<before-round>", TAKES_THIS_ROUND, "--before-set", "<before-set-id>",
        "--after-set", "<set-id>", "--change", "<change>",
    ), purposes=(PURPOSE_BASS,), packet="bass"),
    "delay-landscape": ViewArtifact("delay_landscape.json", purposes=(PURPOSE_SPEAKER,)),
    "close-reference": ViewArtifact("close_reference.json", TAKES_FAR_AND_CLOSE, purposes=(PURPOSE_SPEAKER,)),
    "room": ViewArtifact(ROOM_ARTIFACT, TAKES_SET, purposes=(PURPOSE_ROOM,), bookkeeping=(PURPOSE_ROOM,), builder="round_bookkeeping.room", packet="room"),
    # The packet owns these two names, so the rows take those constants rather
    # than a second spelling of them.
    "distortion": ViewArtifact(
        HARMONICS_ARTIFACT, (TAKES_THIS_ROUND,), in_artifact_dir=True,
        purposes=(PURPOSE_SPEAKER,),
    ),
    "classify-features": ViewArtifact(
        CLASSIFICATION_ARTIFACT, (TAKES_THIS_ROUND,), in_artifact_dir=True,
        purposes=(PURPOSE_SPEAKER,),
    ),
    "findings": ViewArtifact("findings.json"),
    "room-grade": ViewArtifact("room_grade.json", TAKES_SET, purposes=(PURPOSE_ROOM,), bookkeeping=(PURPOSE_ROOM,), grades_against_base=True, builder="round_bookkeeping.room_grade", packet="room"),
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
    "repeat-floor": (),
    "speaker-fit": (PURPOSE_SPEAKER,),
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
