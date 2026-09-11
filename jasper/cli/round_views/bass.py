# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bass views."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jasper.active_speaker.measurement_bass import bass_view
from jasper.active_speaker.bass_comparison import compare_bass_takes, selected_take
from jasper.active_speaker.bass_fit import fit_bass_shape
from jasper.active_speaker.candidate_bank import load_candidate_artifact
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import ARTIFACT_BY_VIEW, _ROUND_TOOL_ERRORS, _write, answer, default_out, resolve_set, round_inputs

def _cmd_bass(args: argparse.Namespace) -> int:
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, args.round_dir)
    selected = resolve_set(inputs, args.set)
    payload = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, bass_view, inputs.session_dir,
                    calibration_root=args.calibration_root)
    payload["takes"] = [take for take in payload["takes"] if take["record"]["take_id"] in selected.selected_ids]
    written = _write(payload, args.out, default_out(inputs, args.round_dir, ARTIFACT_BY_VIEW[args.command].artifact, args.set))
    return answer(args.command, out=written, takes=len(payload["takes"]),
                  line=f"bass: {len(payload['takes'])} take(s) -> {written}")

def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("bass", help="bass response, quiet-window SNR and H2/H3")
    parser.add_argument("round_dir", type=Path)
    parser.add_argument("--set")
    parser.add_argument("--calibration-root", type=Path, help="microphone calibration registry")
    parser.add_argument("--out")
    parser.set_defaults(func=_cmd_bass)
    compare = sub.add_parser("bass-compare", help="compare selected bass sets")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--before-set")
    compare.add_argument("--after-set")
    compare.add_argument("--change", required=True, choices=("candidate", "volume", "demand", "diagnostic"))
    compare.add_argument("--out")
    compare.set_defaults(func=_cmd_compare)
    fit = sub.add_parser("bass-fit", help="fit a measured bass shape to a target")
    fit.add_argument("request", type=Path, help="JSON: candidate, target and take pairs")
    fit.add_argument("--out")
    fit.add_argument("--descriptor-out", type=Path, help="fitted descriptor for compose")
    fit.set_defaults(func=_cmd_fit)

def _cmd_compare(args: argparse.Namespace) -> int:

    before, after = round_inputs(args.before), round_inputs(args.after)
    before_id = resolve_set(before, args.before_set).take_id()
    after_id = resolve_set(after, args.after_set).take_id()
    paths = [default_out(inputs, root, ARTIFACT_BY_VIEW["bass"].artifact, set_id)
             for inputs, root, set_id in ((before, args.before, args.before_set), (after, args.after, args.after_set))]
    def compare():
        takes = [selected_take(json.loads(path.read_text()), take_id) for path, take_id in zip(paths, (before_id, after_id))]
        return {**compare_bass_takes(*takes, change=args.change), "source_views": list(map(str, paths))}
    payload = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, compare)
    written = _write(payload, args.out, default_out(after, args.after, ARTIFACT_BY_VIEW[args.command].artifact, args.after_set))
    return answer(args.command, out=written, available=payload["available"], context=payload["context"],
                  bands=payload["bands"], line=f"bass-compare -> {written}")

def _cmd_fit(args: argparse.Namespace) -> int:

    def fit():
        request = json.loads(args.request.read_text())
        candidate = load_candidate_artifact(args.request.parent / request["candidate"])
        if candidate is None or not candidate.bass_extension:
            raise ValueError("bass_fit_candidate_unreadable")
        def take(ref):
            view = json.loads((args.request.parent / ref["view"]).read_text())
            return selected_take(view, ref["take_id"])
        return fit_bass_shape([(take(pair["before"]), take(pair["after"])) for pair in request["pairs"]],
                              candidate_id=candidate.fingerprint, descriptor=candidate.bass_extension,
                              target=request["target"], reference_band_hz=tuple(request.get("reference_band_hz", [300, 1000])))
    payload = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, fit)
    written = _write(payload, args.out, args.request.parent / ARTIFACT_BY_VIEW[args.command].artifact)
    if args.descriptor_out:
        selected = next(choice for choice in payload["choices"] if choice["scale"] == payload["selected_scale"])
        _write(selected["descriptor"] or {}, str(args.descriptor_out), args.descriptor_out)
    return answer(args.command, out=written, position_count=payload["position_count"], selected_scale=payload["selected_scale"],
                  line=f"bass-fit -> {written}")
