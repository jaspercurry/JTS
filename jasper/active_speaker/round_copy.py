# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The round's words, shared by the coordinator, packet, browser and console."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from .measurement_programs import POSE_KIND_BEHIND, POSE_KIND_CLOSE, POSE_KIND_SEAT
from .movers import MOVER_ARM

CHOOSE_PROGRAM = "Start a measurement round when you are ready."
RUN_ENDED = "The round is complete. No more sound plays until a new round starts."
PLACE_MICROPHONE = "Place the microphone. Confirm it is placed to play this pose's measurements."


def pose_name(pose: Mapping[str, Any]) -> str:
    placement = {POSE_KIND_BEHIND: "behind the speaker", POSE_KIND_CLOSE: "close to the speaker",
                 POSE_KIND_SEAT: "at the seat"}.get(str(pose.get("kind") or ""))
    if placement:
        return placement
    from .crossover_v2.round_frequency_view import position_label  # lazy: only bearing poses need angle words; keeps the CLI parser numpy-free

    label = position_label({"position_deg": pose.get("deg", 0), "vertical_deg": pose.get("elevation_deg", 0)})
    return f"{pose['kind']}: {label}" if pose.get("kind") else label


def pose_line(facts: Mapping[str, Any]) -> str:
    pose = facts["pose_details"][facts["pose"] - 1]
    counts = facts.get("measurements_per_pose") or []
    span = ""
    if counts:
        end = sum(counts[:facts["pose"]])
        start = end - counts[facts["pose"] - 1] + 1
        span = f", measurement {end}" if start == end else f", measurements {start}–{end}"
    return f"Pose {facts['pose']} of {facts['poses']}{span}: {pose_name(pose)} ({facts['mover']})."


def round_verdict(facts: Mapping[str, Any], verdict: str) -> str:
    return "" if facts.get("poses") and not facts.get("status") else verdict


def round_lines(facts: Mapping[str, Any], *, pending: Mapping[str, Any] | bool = False) -> list[str]:
    """``pending`` is the open placement hold, or whether one is open."""
    from .crossover_v2.refusal_copy import refusal_copy_for  # lazy: keeps the CLI parser numpy-free

    lines = []
    if facts.get("status") in {"complete", "partial", "cancelled", "failed", "stopped"}:
        lines = [measured_line(facts.get("takes", 0), facts.get("retakes", 0)),
                 f"Not measured: {_count(facts.get('not_measured', 0), 'planned measurement')}."]
        if facts.get("packet_error"):
            lines.append("The round packet could not be saved. Run jasper-round wait to try again.")
        return lines
    counts = facts.get("measurements_per_pose") or []
    if counts and not facts.get("pose"):
        lines.append(f"Microphone positions: {facts['poses']}.")
        lines.append("Measurements per position: " + ", ".join(str(n) for n in counts)
                     + f"; {_count(facts['measurements'], 'measurement')} in total.")
        lines += ["A measurement that is too quiet can be taken again louder.",
                  f"Allow about {_count(math.ceil(facts['estimated_seconds'] / 60), 'minute')}, plus time for retakes."]
    if facts.get("pose") and facts.get("pose_details"):
        if pending:
            lines += [pose_line(facts), PLACE_MICROPHONE]
        elif facts.get("role"):
            lines.append(f"Measurement {facts['measurement']} of {facts['measurements']}, pose {facts['pose']} of {facts['poses']}.")
            lines.append("Keep the microphone still until the tone stops.")
        else:
            lines += [pose_line(facts), "Preparing this pose's measurements."]
    if reason := facts.get("retake_reason"):
        action = facts.get("retake_action")
        line = (f"Pose {facts['retake_pose']}: you asked to redo this pose." if reason == "operator" else
                f"Pose {facts['retake_pose']}, measurement {facts['retake_measurement']}: {refusal_copy_for(reason)[0]}")
        if pending and action == "fix_and_retake" and facts.get("mover") != MOVER_ARM:
            release = (pending.get("actions") or [{}])[0].get("label") if isinstance(pending, Mapping) else None
            line += f" Press “{release}” to take it again." if release else " Confirm the microphone is in place to take it again."
        elif reason != "operator":
            line += f" Taking it again{' louder' if action == 'retake_louder' else ''}."
        lines.append(line)
    if facts.get("level_raise_dbfs") is not None:
        lines.append(f"Raising the measurement level to {round(facts['level_raise_dbfs'], 1):g} dBFS.")
    return lines + ([PLACE_MICROPHONE] if pending and not facts.get("pose") else [])


def take_counts(document: Mapping[str, Any]) -> dict[str, int]:
    """The ended round's counts, from the manifest ``wait`` reprints once banked."""
    takes = [t for group in document.get("sets", ()) for t in group["takes"]]
    return {"takes": len({t["take_id"] for t in takes if t["selected"]}),
            "retakes": len({t["take_id"] for t in takes if t.get("attempt", 1) > 1}),
            "not_measured": len(document.get("not_measured", ()))}


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def measured_line(count: int, retakes: int = 0) -> str:
    return f"Measured: {_count(count, 'kept take')}. Retakes: {retakes}."


def coverage_lines(packet: Mapping[str, Any], manifest: Mapping[str, Any]) -> list[str]:
    from .crossover_v2.refusal_copy import refusal_copy_for  # lazy: keeps the CLI parser numpy-free

    takes = [t for g in packet.get("sets", ()) for t in g["takes"] if t["selected"]]
    counts = take_counts(manifest)
    lines = [measured_line(counts["takes"], counts["retakes"])]
    poses = list(dict.fromkeys(pose_name(t["pose"]) for t in takes if t.get("pose")))
    if poses:
        lines += ["Measured poses: " + "; ".join(poses) + ".",
                  "Measured roles: " + ", ".join(sorted({t["role"] for t in takes if t.get("role")})) + "."]
    missing: dict[str, list[str]] = {}
    for row in manifest.get("not_measured", ()):
        missing.setdefault(json.dumps(row["pose"], sort_keys=True), []).append(row["reason"])
    for pose, reasons in missing.items():
        name = pose_name(json.loads(pose))
        count_label = f" ({len(reasons)} planned measurements)" if len(reasons) > 1 else ""
        prefix = "Waived" if set(reasons) == {"complete_requested"} else "Not measured"
        details = " ".join(refusal_copy_for(reason)[0] for reason in dict.fromkeys(reasons)
                           if reason != "complete_requested")
        lines.append(f"{prefix}: {name}{count_label}. {details}".rstrip())
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
