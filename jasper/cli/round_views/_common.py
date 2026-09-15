# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What every view here shares: the artifact table, the round reader, the
publisher, the answer printer, and the flags more than one subcommand takes.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME
from jasper.active_speaker.measurement_programs import (
    PURPOSE_BASS,
    PURPOSE_ROOM,
    PURPOSE_SPEAKER,
)
from jasper.active_speaker.frequency_view import FREQUENCY_VIEW_FILENAME
from jasper.active_speaker.crossover_v2.evidence_packet import CLASSIFICATION_ARTIFACT
from jasper.active_speaker.crossover_v2.gate_sweep import DEFAULT_RUNGS_MS
from jasper.active_speaker.crossover_v2.harmonic_evidence import HARMONICS_ARTIFACT
from jasper.active_speaker.crossover_v2.position_cycle import POSITION_CYCLE_FILENAME
from jasper.active_speaker.crossover_v2.round_inputs import (
    RoundSetRefused as RoundSetRefused, SetTakes as SetTakes, read_run_manifest as read_run_manifest,
    resolve_set as resolve_set, ROUND_INPUT_ERRORS as _ROUND_TOOL_ERRORS,
    ROOM_ARTIFACT, RoundInputs, default_out as default_out, set_artifact_name as set_artifact_name,
    round_artifact_dir as round_artifact_dir,
    banked_round_of,
    recent_round_sessions,
    round_inputs,
)
from jasper.active_speaker.crossover_v2.round_views import (
    BankedRound,
    RoundViewsError,
    load_banked_round,
)
from jasper.cli._refusal import (
    answered,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    failed,
    stage,
)
from jasper.cli._report import report_answer, write_report

AUTHORITY_TIER = "advisory (analysis views save artifacts)"

#: What every round-directory positional takes, said once. Both shapes, named
#: in the order an operator meets them: the live one is what a round leaves on
#: the speaker, the banked one is what ``bank-crossover-round.sh`` made of it.
_ROUND_DIR_HELP = "a banked round directory, or a live session bundle"

#: What the usage line calls a directory positional, so ``--help`` reads as
#: the shape to type rather than as this module's own parameter names.
_ROUND_DIR_METAVAR = "<round-dir>"
_BUNDLE_DIR_METAVAR = "<bundle-dir>"

PROG = "jasper-round-views"

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
    ``producer`` names the command for an artifact this tool does NOT write;
    ``None`` means the key is the subcommand that writes it.
    """

    artifact: str
    takes: tuple[str, ...] = (TAKES_THIS_ROUND,)
    in_artifact_dir: bool = False
    producer: str | None = None
    purposes: tuple[str, ...] = ()

#: The artifacts a round carries, declared once: the subcommands take their
#: default output path from this table and ``inventory`` names each one's
#: producer from the same one, so there is no second list to drift.
#: ``repeat-floor`` is absent because it publishes to ``--install`` or
#: ``--out`` instead of beside the round.
ARTIFACT_BY_VIEW: dict[str, ViewArtifact] = {
    "inventory": ViewArtifact("inventory.json", TAKES_SET),
    "run-manifest": ViewArtifact(RUN_MANIFEST_FILENAME, in_artifact_dir=True, producer="plan_run.run_plan"),
    "dsp-replay": ViewArtifact("dsp_replay.json", ("<graph.yml>", "<stimulus.wav>", "--main-db", "<db>", "--bass-reference-db", "<db>", "--out", "<render-dir>")),
    "dsp-levels": ViewArtifact("dsp_levels.json", ("<dsp_replay.json>", "--raw", "<output.f64le>", "--window-s", "<start>", "<stop>")),
    "bass-fit-table": ViewArtifact("bass_table.json", (TAKES_THIS_ROUND, "--candidate", "<candidate.json>", "--target", "<target.json>", "--tolerance-db", "<db>"), purposes=(PURPOSE_BASS,)),
    "entry": ViewArtifact("entry_state_grade.json", purposes=(PURPOSE_SPEAKER,)),
    "frozen": ViewArtifact("frozen_reference.json", TAKES_AFTER_ANOTHER, purposes=(PURPOSE_SPEAKER,)),
    "per-seat": ViewArtifact("per_seat.json", purposes=(PURPOSE_ROOM, PURPOSE_SPEAKER)),
    "repeat": ViewArtifact("repeatability.json", TAKES_BEFORE_ANOTHER),
    "candidates": ViewArtifact("candidates.json"),
    "agreement": ViewArtifact("agreement.json", purposes=(PURPOSE_ROOM, PURPOSE_SPEAKER)),
    "co-metrics": ViewArtifact("audibility_co_metrics.json", purposes=(PURPOSE_ROOM, PURPOSE_SPEAKER)),
    "directivity": ViewArtifact("directivity.json", purposes=(PURPOSE_ROOM, PURPOSE_SPEAKER)),
    "cloud-binding": ViewArtifact("cloud_binding.json", purposes=(PURPOSE_SPEAKER,)),
    "forward-model": ViewArtifact("forward_model.json", TAKES_SET, purposes=(PURPOSE_SPEAKER,)),
    "sweep --scope verdict": ViewArtifact("spec_gate_sensitivity.json", TAKES_SET),
    "sweep --scope round": ViewArtifact("gate_sweep.json", TAKES_SET),
    "sweep --scope take": ViewArtifact("window_view.json", (*TAKES_SET, "--take", "<take-id>")),
    "frequency": ViewArtifact(FREQUENCY_VIEW_FILENAME),
    "bass": ViewArtifact("bass_view.json", TAKES_SET, purposes=(PURPOSE_BASS,)),
    "bass-compare": ViewArtifact("bass_comparison.json", (
        "<before-round>", TAKES_THIS_ROUND, "--before-set", "<before-set-id>",
        "--after-set", "<set-id>", "--change", "<change>",
    ), purposes=(PURPOSE_BASS,)),
    "delay-landscape": ViewArtifact("delay_landscape.json", purposes=(PURPOSE_SPEAKER,)),
    "delay-confirm": ViewArtifact("delay_confirmation.json", purposes=(PURPOSE_SPEAKER,)),
    "close-reference": ViewArtifact("close_reference.json", TAKES_FAR_AND_CLOSE, purposes=(PURPOSE_SPEAKER,)),
    "room": ViewArtifact(ROOM_ARTIFACT, TAKES_SET, purposes=(PURPOSE_ROOM,)),
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
    "room-grade": ViewArtifact("room_grade.json", TAKES_SET, purposes=(PURPOSE_ROOM,)),
    # The banker writes this index; inventory reports its presence.
    "position-cycle": ViewArtifact(
        POSITION_CYCLE_FILENAME, ("--run", "<run-id>"), producer="jasper-round wait",
    ),
}

VIEW_PURPOSES = {
    **{name.split()[0]: spec.purposes for name, spec in ARTIFACT_BY_VIEW.items()},
    "repeat-floor": (),
    "speaker-fit": (PURPOSE_SPEAKER,),
}

INVENTORY_ARTIFACT = ARTIFACT_BY_VIEW["inventory"].artifact

#: The named ``reason`` each failing stage publishes. The bucket is the STAGE,
#: never the exception type — one ``RoundViewsError`` is raised both for a
#: round that could not be read and for a view that declined one, so a
#: type-based split answers the operator's "where do I go" wrong.
REASON_REFUSED = "round_views_refused"
REASON_UNREADABLE = "round_views_unreadable_round"
REASON_UNWRITABLE = "round_views_unwritable_out"

_REASON_BY_CODE = {
    EXIT_REFUSED: REASON_REFUSED,
    EXIT_UNREADABLE: REASON_UNREADABLE,
    EXIT_WRITE_FAILED: REASON_UNWRITABLE,
}


def _load_round(round_dir: str | Path) -> BankedRound:
    """Read one round directory. A failure here is the ROUND, not the view."""

    return stage(
        EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, load_banked_round, Path(round_dir)
    )


def _write(
    payload: Any, out: str | Path | None, default_path: Path, *, make_parents: bool = False,
) -> Path:
    """Publish one view; only filesystem errors become write refusals."""

    return stage(
        EXIT_WRITE_FAILED, (OSError,), write_report, payload, out, default_path,
        make_parents=make_parents,
    )


def answer(view: str, *, out: Path | None = None, line: str, **fields: Any) -> int:
    """Print scalar results and an artifact pointer (ADR-0237)."""
    return answered(report_answer(view, out, **fields), line)


def refused_by_name(
    reason: str, detail: Mapping[str, Any] | str, *, code: int = EXIT_REFUSED
) -> int:
    """An instrument that refuses BY NAME publishes its own name and its
    evidence here — fields, or a sentence — never this tool's stage bucket."""
    if not isinstance(detail, str):
        detail = json.dumps(detail, sort_keys=True, default=str)
    return failed(code, reason, detail)


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


def resolved_out(round_dir: Path, artifact: str, set_id: str | None = None) -> Path:
    """Where a view lands beside a round it read WITHOUT the round resolver.

    A round the resolver cannot place still gets its artifact, beside itself.
    """
    try:
        return default_out(round_inputs(round_dir), round_dir, artifact, set_id)
    except RoundViewsError:
        return round_dir / set_artifact_name(artifact, set_id)


def _view_out(args: argparse.Namespace, round_: BankedRound) -> Path:
    """This subcommand's own artifact path, from :data:`ARTIFACT_BY_VIEW`."""
    return default_out(
        round_.inputs, round_.round_dir, ARTIFACT_BY_VIEW[args.command].artifact, getattr(args, "set", None)
    )


def add_set_argument(
    parser: argparse.ArgumentParser, *, name: str = "--set", required: bool = False,
    take: bool = False,
) -> None:
    parser.add_argument(name, required=required, help="set in the run manifest; optional for a one-set round")
    if not required:
        parser.set_defaults(optional_set_flags=(*(parser.get_default("optional_set_flags") or ()), name))
    if take:
        parser.add_argument(name.removesuffix("set") + "take", help=f"selected take within {name}; defaults to the unique on-axis take")


def add_rungs_ms_argument(
    parser: argparse.ArgumentParser, *, flag: str = "--rungs-ms",
    dest: str | None = None, repeatable: bool = False,
) -> None:
    """The gate ladder flag, so the shipped ladder has one owner.

    ``repeatable=True`` defaults to ``None``, never the shipped rungs: an
    argparse ``append`` over a non-empty default ADDS to it, not replaces it.
    """
    shipped = " ".join(f"{r:g}" for r in DEFAULT_RUNGS_MS)
    if repeatable:
        parser.add_argument(
            flag, type=float, action="append", default=None, dest=dest,
            metavar="MS",
            help=f"one rung in ms, repeatable; replaces the ladder ({shipped})",
        )
        return
    parser.add_argument(
        flag, type=float, nargs="+", default=list(DEFAULT_RUNGS_MS), dest=dest,
        metavar="MS", help=f"gate ladder, in milliseconds (default: {shipped})",
    )


def _add_norm_band_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--norm-lo", type=float, default=400.0, help="normalisation band low edge, Hz (default 400)")
    parser.add_argument("--norm-hi", type=float, default=8000.0, help="normalisation band high edge, Hz (default 8000)")
