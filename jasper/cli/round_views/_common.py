# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What every view here shares: the artifact table, the round reader, the
publisher, and the flags more than one subcommand takes.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

from jasper.active_speaker.crossover_v2.evidence_packet import CLASSIFICATION_ARTIFACT
from jasper.active_speaker.crossover_v2.gate_sweep import DEFAULT_RUNGS_MS
from jasper.active_speaker.crossover_v2.harmonic_evidence import HARMONICS_ARTIFACT
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, round_inputs
from jasper.active_speaker.crossover_v2.round_views import (
    BankedRound,
    RoundViewsError,
    load_banked_round,
)
from jasper.cli._refusal import (
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    failed,
    stage,
)
from jasper.cli._report import write_report

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "advisory (classify-features projects a ring into the bundle)"

#: What every round-directory positional takes, said once. Both shapes, named
#: in the order an operator meets them: the live one is what a round leaves on
#: the speaker, the banked one is what ``bank-crossover-round.sh`` made of it.
_ROUND_DIR_HELP = "a banked round directory, or a live session bundle"

PROG = "jasper-round-views"

#: The positionals a view's subcommand takes, with the inventoried round in
#: the SLOT that writes the artifact beside it: ``frozen`` grades — and writes
#: beside — the TARGET, ``repeat`` writes beside the FIRST directory, so their
#: second round sits on opposite sides. A hint carrying ``<other-round>`` is
#: one this inventory cannot fill, and running it without that round is an
#: invocation argparse rejects — and so is one carrying inputs that live
#: outside the round tree at all.
TAKES_THIS_ROUND = "<this-round>"
TAKES_AFTER_ANOTHER = "<other-round> <this-round>"
TAKES_BEFORE_ANOTHER = "<this-round> <other-round>"
TAKES_THIS_BUNDLE = "<this-round's bundle>"
TAKES_BUNDLE_AND_RING = f"{TAKES_THIS_BUNDLE} --dumps <ring> --state <flow-state>"
TAKES_FAR_AND_CLOSE = "--far-round <this-round> --close-round <other-round> --close-m M"


class ViewArtifact(NamedTuple):
    """One view's artifact, what its subcommand takes, and where it lands.

    ``in_artifact_dir`` marks the views the evidence PACKET reads: those file
    into the round's own artifact directory, the only path that reader looks
    at, rather than beside the round where an operator reads the rest.
    """

    artifact: str
    takes: str = TAKES_THIS_ROUND
    in_artifact_dir: bool = False

#: The artifact each view writes beside the round, declared once: the
#: subcommands take their default output path from this table and
#: ``inventory`` names each missing artifact's producer from the same one, so
#: there is no second list to drift. ``repeat-floor`` is absent because it
#: publishes to ``--install`` or ``--out`` instead of beside the round.
ARTIFACT_BY_VIEW: dict[str, ViewArtifact] = {
    "entry": ViewArtifact("entry_state_grade.json"),
    "frozen": ViewArtifact("frozen_reference.json", TAKES_AFTER_ANOTHER),
    "per-seat": ViewArtifact("per_seat.json"),
    "repeat": ViewArtifact("repeatability.json", TAKES_BEFORE_ANOTHER),
    "agreement": ViewArtifact("agreement.json"),
    "co-metrics": ViewArtifact("audibility_co_metrics.json"),
    "directivity": ViewArtifact("directivity.json"),
    "cloud-binding": ViewArtifact("cloud_binding.json"),
    "forward-model": ViewArtifact("forward_model.json"),
    "spec-sweep": ViewArtifact("spec_gate_sensitivity.json"),
    "gate-sweep": ViewArtifact("gate_sweep.json"),
    "frequency": ViewArtifact("frequency_view.json"),
    "close-reference": ViewArtifact("close_reference.json", TAKES_FAR_AND_CLOSE),
    # The packet owns these two names, so the rows take those constants rather
    # than a second spelling of them.
    "distortion": ViewArtifact(
        HARMONICS_ARTIFACT, TAKES_BUNDLE_AND_RING, in_artifact_dir=True
    ),
    "classify-features": ViewArtifact(
        CLASSIFICATION_ARTIFACT, TAKES_THIS_BUNDLE, in_artifact_dir=True
    ),
}

#: ``inventory``'s own report, named apart from the views it reports on.
INVENTORY_ARTIFACT = "inventory.json"

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


def _write(payload: Any, out: str | None, default_path: Path) -> Path | None:
    """Publish one view. ``OSError`` only, and that is the whole rule.

    A ``ValueError`` out of the strict writer is a payload this run should not
    have built — co-metrics over partial bearing coverage yields ``NaN``, which
    ``allow_nan=False`` rejects — and sending that operator to fix the
    filesystem sends them to the wrong place. It falls to :func:`main`.
    """

    return stage(EXIT_WRITE_FAILED, (OSError,), write_report, payload, out, default_path)


def refused_by_name(
    reason: str, detail: Mapping[str, Any], *, code: int = EXIT_REFUSED
) -> int:
    """An instrument that refuses BY NAME publishes its own name and its
    evidence here, never this tool's coarser stage bucket."""
    return failed(code, reason, json.dumps(detail, sort_keys=True, default=str))


def default_out(inputs: RoundInputs, round_dir: Path, name: str) -> Path:
    """Where a view lands when the operator named no ``--out``.

    A BANKED round tree is the operator's own directory, so its views stay
    beside the evidence they were computed from. A LIVE session bundle is the
    daemon's (``/var/lib/jasper/active_speaker/sessions/<id>``, written by the
    web host as its own user): defaulting inside it made the ordinary
    invocation — grade the round I just ran — raise ``PermissionError`` for
    the operator this door was added for (#3498). So a live round's view lands
    beside the caller instead, named by the session it came from so two
    sessions graded in one directory do not overwrite each other.
    """
    if inputs.banked:
        return round_dir / name
    return Path.cwd() / f"{inputs.session_dir.name}-{name}"


def resolved_out(round_dir: Path, artifact: str) -> Path:
    """Where a view lands beside a round it read WITHOUT the round resolver.

    The gate ladder and the close reference read a bundle subtree the resolver
    cannot place; a round it cannot place still gets its artifact.
    """
    try:
        return default_out(round_inputs(round_dir), round_dir, artifact)
    except RoundViewsError:
        return round_dir / artifact


def _view_out(args: argparse.Namespace, round_: BankedRound) -> Path:
    """This subcommand's own artifact path, from :data:`ARTIFACT_BY_VIEW`."""
    return default_out(
        round_.inputs, round_.round_dir, ARTIFACT_BY_VIEW[args.command].artifact
    )


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
