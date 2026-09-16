# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The round's words, shared by the coordinator, packet, browser and console."""
from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

ROUND_LABELS = {"position_ready": "Microphone is placed — play", "retake": "Redo this pose",
                "reset_round": "Reset the round", "run_program": "Start the round", "done": "Finish with these poses",
                "choose_program": "Choose a pose set, then start the round.",
                "run_ended": "This run has ended. Choose a pose set to start the next one."}


def _reason(code: str) -> str:
    from .crossover_v2.refusal_copy import refusal_copy_for  # lazy: keeps the CLI parser numpy-free

    return refusal_copy_for(code)[0]


def pose_name(pose: Mapping[str, Any]) -> str:
    az, el = pose.get("deg", 0), pose.get("elevation_deg", 0)
    bearing = f"{abs(az):g}° {'left' if az < 0 else 'right'}" if az else "0°"
    height = f"{abs(el):g}° {'up' if el > 0 else 'down'}" if el else "ear height"
    return f"{bearing}, {height}"


def pose_line(facts: Mapping[str, Any]) -> str:
    return f"Pose {facts['pose']} of {facts['poses']}: {pose_name(facts.get('pose_detail') or {})} ({facts.get('mover', 'human')})."


def round_lines(facts: Mapping[str, Any], *, pending: bool = False) -> list[str]:
    lines = []
    if facts.get("status") in {"complete", "partial", "cancelled", "failed", "stopped"}:
        lines = [f"Measured: {facts.get('takes', 0)} takes; not measured: {facts.get('not_measured', 0)} planned captures."]
        if facts.get("packet_error"):
            lines.append("The round packet could not be saved. Run jasper-round wait to try again.")
        return lines
    counts = facts.get("sweeps_per_pose") or []
    if counts and not facts.get("pose"):
        lines.append(f"Pose set {facts['program']}: {facts['poses']} poses ({facts['mover']}).")
        lines.append("Sweeps per pose: " + ", ".join(str(n) for n in counts) + f"; {facts['sweeps']} sweeps in total.")
        for index, sweeps in enumerate(facts["pose_sweeps"], 1):
            groups = Counter((row["role"], row["kind"]) for row in sweeps)
            parts = [f"{n} {role} {'preparation ' if kind == 'pilot' else ''}{'sweep' if n == 1 else 'sweeps'}"
                     for (role, kind), n in groups.items()]
            lines.append(f"Pose {index}: " + "; ".join(parts) + ".")
        repeats = sorted({s["program_repeats"] for pose in facts["pose_sweeps"] for s in pose
                          if s["phase"] == "measure" and s["kind"] == "sweep"})
        if repeats:
            lines.append("/".join(str(n) for n in repeats) + " repeats per driver, to measure the noise floor.")
        if facts.get("timing_sweeps"):
            lines.append(f"Timing sweeps at 0°: {facts['timing_sweeps']} (included in the totals).")
        if facts.get("preparation_sweeps"):
            lines.append(f"Preparation sweeps: {facts['preparation_sweeps']} (included in the totals).")
        lines += ["A sweep that is too quiet can be taken again louder.",
                  f"Allow about {math.ceil(facts['estimated_seconds'] / 60)} minutes, plus time for retakes."]
    if facts.get("pose") and facts.get("poses"):
        if pending:
            lines += [pose_line(facts), "Place the microphone. Confirm it is placed to play this pose's sweeps."]
        elif facts.get("role"):
            kind = " preparation" if facts.get("sweep_kind") == "pilot" else ""
            lines.append(f"Pose {facts['pose']} of {facts['poses']}, sweep {facts['sweep']} of {facts['sweep_total']}: "
                         f"{facts['role']}{kind} repeat {facts['repeat']} of {facts['repeats']}.")
            lines.append("Keep the microphone still until the tone stops.")
        else:
            lines += [pose_line(facts), "Preparing this pose's sweeps."]
    if facts.get("retake_reason") == "operator":
        lines.append(f"Pose {facts['retake_pose']}: you asked to redo this pose.")
    elif facts.get("retake_reason"):
        reason = _reason(facts["retake_reason"])
        louder = " louder" if facts.get("retake_action") == "retake_louder" else ""
        first, last = facts["retake_sweep"], facts.get("retake_sweep_end", facts["retake_sweep"])
        sweep = f"sweeps {first}–{last}" if first != last else f"sweep {first}"
        lines.append(f"Pose {facts['retake_pose']}, {sweep}: {reason} Taking it again{louder}.")
    if facts.get("level_raise_dbfs") is not None:
        lines.append(f"Raising the sweep level to {facts['level_raise_dbfs']:g} dBFS.")
    return lines


def coverage_lines(packet: Mapping[str, Any], manifest: Mapping[str, Any]) -> list[str]:
    takes = [t for g in packet.get("sets", ()) for t in g["takes"] if t["selected"]]
    count = len({t["take_id"] for t in takes})
    lines = [f"Measured: {count} {'take' if count == 1 else 'takes'}."]
    poses = list(dict.fromkeys(pose_name(t["pose"]) for t in takes if t.get("pose")))
    if poses:
        lines += ["Measured poses: " + "; ".join(poses) + ".",
                  "Measured roles: " + ", ".join(sorted({t["role"] for t in takes if t.get("role")})) + "."]
    lines += [f"Waived: {pose_name(row['pose'])}." if row["reason"] == "complete_requested" else
              f"Not measured: {pose_name(row['pose'])}. {_reason(row['reason'])}"
              for row in manifest.get("not_measured", ())]
    lines += list(dict.fromkeys(f"Unqualified band ({t['role']}): below {t['trusted_floor_hz']:g} Hz."
                               for t in takes if t.get("trusted_floor_hz") is not None))
    lines += [str(line) for line in packet.get("disclosures", ())]
    if packet.get("next_action"):
        lines.append(packet["next_action"]["label"])
    return lines


def packet_lines(directory: str) -> list[str]:
    try:
        packet = json.loads((Path(directory) / "packet.json").read_text())
        manifest = json.loads(Path(packet["artifacts"]["manifest"]).read_text())
    except (OSError, ValueError, KeyError):
        return []
    return coverage_lines(packet, manifest)
