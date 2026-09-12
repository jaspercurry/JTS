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
from jasper.active_speaker.measurement_programs import POSE_KIND_BEARING
from jasper.active_speaker.crossover_v2.evidence_packet import CLASSIFICATION_ARTIFACT
from jasper.active_speaker.crossover_v2.gate_sweep import DEFAULT_RUNGS_MS
from jasper.active_speaker.crossover_v2.harmonic_evidence import HARMONICS_ARTIFACT
from jasper.active_speaker.crossover_v2.position_cycle import POSITION_CYCLE_FILENAME
from jasper.active_speaker.crossover_v2.round_inputs import (
    ROOM_ARTIFACT, RoundInputs, default_out as default_out, set_artifact_name as set_artifact_name,
    round_artifact_dir,
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
    "bass-fit-table": ViewArtifact("bass_table.json", (TAKES_THIS_ROUND, "--run", "<run-id>", "--candidate", "<candidate.json>", "--target", "<target.json>", "--tolerance-db", "<db>")),
    "packet": ViewArtifact("packet.json", producer="jasper-crossover-prescriber packet"),
    "entry": ViewArtifact("entry_state_grade.json"),
    "frozen": ViewArtifact("frozen_reference.json", TAKES_AFTER_ANOTHER),
    "per-seat": ViewArtifact("per_seat.json"),
    "repeat": ViewArtifact("repeatability.json", TAKES_BEFORE_ANOTHER),
    "candidates": ViewArtifact("candidates.json"),
    "agreement": ViewArtifact("agreement.json"),
    "co-metrics": ViewArtifact("audibility_co_metrics.json"),
    "directivity": ViewArtifact("directivity.json"),
    "cloud-binding": ViewArtifact("cloud_binding.json"),
    "forward-model": ViewArtifact("forward_model.json", TAKES_SET),
    "sweep --scope verdict": ViewArtifact("spec_gate_sensitivity.json", TAKES_SET),
    "sweep --scope round": ViewArtifact("gate_sweep.json", TAKES_SET),
    "sweep --scope take": ViewArtifact("window_view.json", (*TAKES_SET, "--take", "<take-id>")),
    "frequency": ViewArtifact("frequency_view.json"),
    "bass": ViewArtifact("bass_view.json", TAKES_SET),
    "bass-compare": ViewArtifact("bass_comparison.json", (
        "<before-round>", TAKES_THIS_ROUND, "--before-set", "<before-set-id>",
        "--after-set", "<set-id>", "--change", "<change>",
    )),
    "delay-landscape": ViewArtifact("delay_landscape.json"),
    "delay-confirm": ViewArtifact("delay_confirmation.json"),
    "close-reference": ViewArtifact("close_reference.json", TAKES_FAR_AND_CLOSE),
    "room": ViewArtifact(ROOM_ARTIFACT, TAKES_SET),
    # The packet owns these two names, so the rows take those constants rather
    # than a second spelling of them.
    "distortion": ViewArtifact(
        HARMONICS_ARTIFACT, (TAKES_THIS_ROUND,), in_artifact_dir=True
    ),
    "classify-features": ViewArtifact(
        CLASSIFICATION_ARTIFACT, (TAKES_THIS_ROUND,), in_artifact_dir=True
    ),
    "findings": ViewArtifact("findings.json"),
    "room-grade": ViewArtifact("room_grade.json", TAKES_SET),
    # No view writes this one: the banker does, as it files the session. It is
    # inventoried anyway because "does this round carry its pose index" is the
    # same question as the rest, asked of the same directory.
    "position-cycle": ViewArtifact(
        POSITION_CYCLE_FILENAME, (TAKES_THIS_BUNDLE,), producer="jasper-round bank",
    ),
}

INVENTORY_ARTIFACT = ARTIFACT_BY_VIEW["inventory"].artifact

#: A round directory is operator-pulled evidence, not a validated
#: input — the documented failure shapes it can hand back are broader than
#: the product module's own typed :class:`RoundViewsError`. A malformed
#: evidence document can be missing a key (``KeyError``), hold the wrong type
#: at one (``TypeError``), or not parse at all (``ValueError``, which
#: ``json.JSONDecodeError`` subclasses); and any of the files this tool reads
#: — or the one it WRITES, where an operator can name an ``--out`` they may
#: not create — can simply not exist or not be permitted (``OSError``, which
#: ``PermissionError`` subclasses). The LOAD stage claims this whole tuple;
#: :func:`main` takes what no stage claimed, so no subcommand can grow a
#: traceback of its own.
#:
#: ``struct.error`` was here for one reader that no longer exists: a
#: header-truncated dump-ring WAV raised it out of ``scipy.io.wavfile.read``
#: while ``verify_pose_curve`` still deconvolved raw ring bytes. That view
#: reads the round's banked curve now, no code on this path opens a WAV, and
#: catching an exception nothing can raise is not how it is caught.
_ROUND_TOOL_ERRORS: tuple[type[Exception], ...] = (
    RoundViewsError, OSError, EOFError, ValueError, KeyError, TypeError,
)

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
    """Publish one view. ``OSError`` only, and that is the whole rule.

    A ``ValueError`` out of the strict writer is a payload this run should not
    have built — co-metrics over partial bearing coverage yields ``NaN``, which
    ``allow_nan=False`` rejects — and sending that operator to fix the
    filesystem sends them to the wrong place. It falls to :func:`main`.
    """

    return stage(
        EXIT_WRITE_FAILED, (OSError,), write_report, payload, out, default_path,
        make_parents=make_parents,
    )


def answer(view: str, *, out: Path | None = None, line: str, **fields: Any) -> int:
    """Print scalar results and an artifact pointer (ADR-0237)."""
    return answered(report_answer(view, out, **fields), line)


class RoundSetRefused(ValueError):
    def __init__(self, reason: str, **detail: Any) -> None:
        self.reason, self.detail = reason, detail
        super().__init__(reason)


class SetTakes(NamedTuple):
    set_id: str
    capture_basis: Mapping[str, Any]
    takes: tuple[Mapping[str, Any], ...]

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(take["take_id"] for take in self.takes if take["selected"])

    def take_id(self, requested: str | None = None) -> str:
        ids = self.selected_ids
        if requested is not None:
            if requested not in ids:
                raise RoundSetRefused("round_take_unknown", set_id=self.set_id, take_id=requested, take_ids=ids)
            return requested
        if len(ids) == 1:
            return ids[0]
        on_axis = [take["take_id"] for take in self.takes if take["selected"]
                   and take["pose"].get("kind") == POSE_KIND_BEARING
                   and take["pose"].get("deg") == 0 and take["pose"].get("elevation_deg") == 0]
        if len(on_axis) == 1:
            return on_axis[0]
        raise RoundSetRefused("round_take_selection_required", set_id=self.set_id, take_ids=ids)


def read_run_manifest(
    inputs: RoundInputs, *, manifest: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    if manifest is None:
        directory, _ = round_artifact_dir(inputs.session_dir)
        path = directory / RUN_MANIFEST_FILENAME if directory else inputs.session_dir / RUN_MANIFEST_FILENAME
        if directory is None or not path.is_file():
            raise RoundSetRefused("round_manifest_missing", path=str(path))
        manifest = json.loads(path.read_text())
    assert manifest is not None
    if manifest.get("finalized") is not True:
        raise RoundSetRefused("round_manifest_unfinalized", run_id=manifest.get("run_id"))
    return manifest


def resolve_set(
    inputs: RoundInputs, set_id: str | None = None, *, manifest: Mapping[str, Any] | None = None,
) -> SetTakes:
    """Resolve the executor's set without rebuilding its identity (ADR-0299)."""
    sets = read_run_manifest(inputs, manifest=manifest)["sets"]
    matches = [row for row in sets if set_id is None or row["set_id"] == set_id]
    if len(matches) != 1:
        raise RoundSetRefused("round_set_unknown", set_id=set_id, sets=[row["set_id"] for row in sets])
    row, = matches
    return SetTakes(row["set_id"], row["capture_basis"], tuple(row["takes"]))


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
    packet = default_out(inputs, round_dir, ARTIFACT_BY_VIEW["packet"].artifact)
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
        for key, path in (("frozen_packet", packet), ("latest_agent_note", latest_note))
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
