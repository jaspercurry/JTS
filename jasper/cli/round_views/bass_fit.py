# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""How deep this room lets the bass go, and what each rung of it costs.

* ``bass-fit <round-dir>`` — fit the bass plant on the seat-cube median banked
  beside this round (``room_median.json``, or ``--median``), against the
  cabinet the design draft's ``driver_safety_profile`` declares for the bass
  owner, and publish the extended-corner family that plant supports: each
  rung's target, its transform, its boost, and the maximum listening level
  that boost costs. Writes ``bass_fit.json``.

The median is read through the room door's own reader
(:mod:`~jasper.active_speaker.crossover_v2.room_prescription`), so a median
fitted here is exactly one that door would prescribe against — a gated or
mixed median refuses by the door's own name rather than being fitted.

The fit is in situ — the median carries room gain, and that is the point
(ADR-0260 section 3). Offline: no audio plays and no device is opened. Nothing
here applies a rung; sizing the family and installing one are different doors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from jasper.active_speaker.crossover_v2.room_prescription import (
    RoomMedian,
    RoomPrescriptionRefused,
    read_room_median,
)
from jasper.active_speaker.crossover_v2.round_inputs import (
    DRIVERS_DEFAULT_PATH,
    round_inputs,
)
from jasper.active_speaker.design_draft import load_design_draft
from jasper.bass_extension.profile import BassExtensionRefusal
from jasper.bass_extension.seat_fit import (
    DeclaredPlant,
    SeatFitRefused,
    bass_owner_target,
    cabinet_of,
    fit_seat_median,
)
from jasper.bass_extension.targets import MARGINS
from jasper.cli._refusal import EXIT_UNREADABLE, read_source_bytes, stage
from jasper.json_fields import sha256_file

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    default_out,
    refused_by_name,
)

#: The room-median view's artifact is this view's input.
MEDIAN_FILENAME = ARTIFACT_BY_VIEW["room-median"].artifact


def _median(path: Path) -> tuple[RoomMedian, str]:
    """One median document as a value, and the digest of the bytes it came
    from — how a later door names the median this family was sized on.

    A missing or unparsable file is the ROUND failing to carry its median,
    which is the load stage's code; a document the room door will not read is
    that door's own refusal, raised through.
    """
    raw = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, read_source_bytes, str(path))
    document = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, json.loads, raw)
    return read_room_median(document), hashlib.sha256(raw).hexdigest()


def _cmd_bass_fit(args: argparse.Namespace) -> int:
    # A datasheet plant is a pair; half of one describes nothing, and argparse
    # cannot mark either half required without requiring both always.
    if (args.declared_f0_hz is None) != (args.declared_q0 is None):
        args.parser.error("--declared-f0-hz and --declared-q0 are declared together")
    round_dir = Path(args.round_dir)
    inputs = round_inputs(round_dir)
    median_path = (
        Path(args.median) if args.median
        else default_out(inputs, round_dir, MEDIAN_FILENAME)
    )
    draft_path = (
        Path(args.design_draft) if args.design_draft
        else (inputs.design_draft_path or DRIVERS_DEFAULT_PATH)
    )

    try:
        median, median_sha256 = _median(median_path)
    except RoomPrescriptionRefused as exc:
        # A document that will not read into a median is the INPUT failing, not
        # this view declining a round it read.
        return refused_by_name(exc.reason, exc.detail, code=EXIT_UNREADABLE)

    draft = load_design_draft(draft_path)
    if draft.get("status") == "unreadable":
        return refused_by_name(
            "design_draft_unreadable", {"design_draft": str(draft_path)},
            code=EXIT_UNREADABLE,
        )
    try:
        profile = draft.get("driver_safety_profile")
        if not isinstance(profile, Mapping):
            # ``load_design_draft`` fails soft, so a draft that was never saved
            # arrives as a draft carrying no profile — its status separates the
            # two.
            raise SeatFitRefused(BassExtensionRefusal.ENCLOSURE_UNKNOWN, {
                "design_draft": str(draft_path),
                "draft_status": draft.get("status"),
                "problem": "no_driver_safety_profile",
            })
        target = bass_owner_target(profile)
        adapter, cabinet = cabinet_of(target)
        fit = fit_seat_median(
            median,
            adapter=adapter,
            cabinet=cabinet,
            margin=MARGINS[args.margin],
            declared=(
                None if args.declared_f0_hz is None
                else DeclaredPlant(args.declared_f0_hz, args.declared_q0)
            ),
            owner_role=str(target.get("role") or ""),
            owner_target_id=str(target.get("target_id") or ""),
        )
    except SeatFitRefused as exc:
        return refused_by_name(exc.reason, exc.detail)

    published = fit.to_dict()
    published["median"] = {"path": str(median_path), "sha256": median_sha256}
    published["design_draft"] = {
        "path": str(draft_path), "sha256": sha256_file(draft_path),
    }
    published["cabinet"] = asdict(cabinet)
    written = _write(
        {"status": "fitted", "bass_fit": published},
        args.out,
        default_out(inputs, round_dir, ARTIFACT_BY_VIEW[args.command].artifact),
    )
    deepest = min(fit.rungs, key=lambda rung: rung.target.fp_hz)
    return answer(
        args.command, out=written,
        effective_corner_hz=fit.effective_corner_hz, effective_q=fit.effective_q,
        fit_rms_db=fit.fit_rms_db, plant_source=fit.plant_source,
        adapter_id=fit.adapter_id, owner_role=fit.owner_role, margin=fit.margin,
        ceiling_hz=fit.ceiling_hz, rung_count=len(fit.rungs),
        deepest_target_id=deepest.target.target_id,
        deepest_fp_hz=deepest.target.fp_hz,
        deepest_lt_boost_db=deepest.lt_boost_db,
        deepest_boost_headroom_db=deepest.target.boost_headroom_db,
        deepest_max_listening_level=deepest.max_listening_level,
        line=(
            f"bass-fit: {fit.plant_source} corner {fit.effective_corner_hz:g} Hz"
            + (f" Q {fit.effective_q:.2f}" if fit.effective_q is not None else "")
            + f"; {len(fit.rungs)} rung(s), deepest {deepest.target.target_id} at "
            f"{deepest.target.fp_hz:g} Hz (+{deepest.lt_boost_db:.1f} dB, costs "
            f"{deepest.target.boost_headroom_db:.1f} dB, level "
            f"{deepest.max_listening_level})"
            + (f" -> {written}" if written else "")
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    bass = sub.add_parser(
        "bass-fit",
        help="fit the bass plant on this round's seat-cube median and size the extension family it supports",
    )
    bass.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    bass.add_argument(
        "--margin", choices=tuple(MARGINS), default="conservative",
        help="boost cap and digital margin policy (default: conservative)",
    )
    bass.add_argument(
        "--median", default=None, metavar="PATH",
        help=f"the seat-cube median (default: <round-dir>/{MEDIAN_FILENAME})",
    )
    bass.add_argument(
        "--design-draft", default=None, metavar="PATH",
        help="the draft whose driver_safety_profile declares the cabinet "
             "(default: the one banked beside this round, else the household's)",
    )
    bass.add_argument(
        "--declared-f0-hz", type=float, default=None, metavar="F",
        help="datasheet sealed-box corner, used ONLY if the fit refuses; "
             "takes --declared-q0 with it",
    )
    bass.add_argument(
        "--declared-q0", type=float, default=None, metavar="Q",
        help="datasheet sealed-box Q, declared with --declared-f0-hz",
    )
    bass.add_argument("--out", default=None, help="write the result here (- for stdout)")
    bass.set_defaults(func=_cmd_bass_fit, parser=bass)
