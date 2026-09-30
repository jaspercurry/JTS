# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""What to run next on one round: each catalog tool that reads it, called on its sets and takes (#5928 TB5)."""
from __future__ import annotations

import shlex
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from .crossover_v2.position_cycle import take_curve
from .crossover_v2.round_inputs import SetTakes, set_artifact_name, take_artifact_name
from .measurement_programs import PURPOSE_ROOM, PURPOSE_SPEAKER, PURPOSES, run_purposes
from .round_view_artifacts import CATALOG, PROG, TAKES_THIS_ROUND, CatalogRow, read_purposes
from .run_manifest import room_sets, view_sets

#: The placeholders a round fills; any other one names an input the round does not hold.
_FILLED = frozenset({TAKES_THIS_ROUND, "<set-id>", "<take-id>", "<program>"})


def catalog_command(round_dir: Path | str) -> str:
    """The call that lists what to run next on a round (ADR-0393)."""
    return shlex.join([PROG, "catalog", str(round_dir)])


def _first_takes(group: SetTakes) -> list[Mapping[str, Any]]:
    """The set's first kept take of each pose kind that holds its response, on-axis first."""
    firsts: dict[Any, Mapping[str, Any]] = {}
    for take in (*group.on_axis, *group.takes):
        if take["selected"] and take_curve(take, group.role):
            firsts.setdefault(take["pose"].get("kind"), take)
    return list(firsts.values())


def _call(command: str, row: CatalogRow, round_dir: Path, *, set_id: str | None = None, take_id: str | None = None,
          role: str = "", program: str | None = None, one_set: bool = False,
          needs: Sequence[str] = ()) -> dict[str, Any]:
    """One call of ``row``, and the file its argv writes beside the round."""
    words = list(row.argv)
    if one_set and row.bookkeeping and "--set" in words:
        at = words.index("--set")
        del words[at:at + 2]
    fill = {TAKES_THIS_ROUND: str(round_dir), "<set-id>": set_id, "<take-id>": take_id, "<program>": program}
    artifact = None
    if row.artifact and not needs:
        artifact = (take_artifact_name(row.artifact, take_id, role) if row.per_take and take_id
                    else set_artifact_name(row.artifact, set_id if "<set-id>" in words else None))
    return {"tool": command, "argv": [*shlex.split(command), *(fill.get(word) or word for word in words)],
            "set_id": set_id, "take_id": take_id, "needs": list(needs), "artifact": artifact}


def round_calls(round_dir: Path, manifest: Mapping[str, Any], *, purposes: Collection[str] = PURPOSES,
                set_id: str | None = None) -> list[dict[str, Any]]:
    """Every call of a catalog tool that reads this round, its argv filled from the round.

    ``<this-round>`` is the round; ``<set-id>`` each set whose purpose the tool's
    programs read, less the base for a tool that grades against it and a summed
    set for one that reads ``driver_sets``; ``<take-id>`` that set's
    :func:`_first_takes`; ``<program>`` each program the round serves. On a
    one-set round a view the bank publishes leaves out ``--set``, so it files
    what the bank filed; every other call names its set, since some views read
    other takes without one.
    A tool that also needs an input no round holds (another round, a document)
    is one call, those inputs left in ``needs``. ``manifest`` is joined with its
    records; ``purposes`` and ``set_id`` narrow the calls.
    """
    if not manifest.get("preset"):
        return []
    served = run_purposes(manifest["preset"])
    rooms = {entry["set_id"] for entry in room_sets(manifest)} if served[0] == PURPOSE_SPEAKER else set()
    wanted = read_purposes(served, has_room=bool(rooms)).intersection(purposes)
    sets = view_sets(manifest)
    groups = [(group, {PURPOSE_ROOM} if group.set_id in rooms else set(served), bool(entry.get("base")), _first_takes(group))
              for entry in sets if set_id in (None, entry["set_id"]) for group in [SetTakes.from_row(entry)]]
    calls = []
    for command, row in CATALOG.items():
        reads = wanted.intersection(row.programs or PURPOSES)
        holes = list(dict.fromkeys(word for word in row.argv if word.startswith("<")))
        if TAKES_THIS_ROUND not in holes or not reads:
            continue
        if not _FILLED.issuperset(holes):
            calls.append(_call(command, row, round_dir, needs=[hole for hole in holes if hole != TAKES_THIS_ROUND]))
        elif "<program>" in holes:
            calls += [_call(command, row, round_dir, program=program) for program in PURPOSES if program in reads]
        elif "<set-id>" in holes:
            calls += [_call(command, row, round_dir, set_id=group.set_id, take_id=take["take_id"] if take else None,
                            role=group.role, one_set=len(sets) == 1)
                      for group, of, base, firsts in groups if reads & of and not (base and row.grades_against_base)
                      and not (row.driver_sets and group.role == "summed")
                      for take in (firsts if "<take-id>" in holes else [None])]
        else:
            calls.append(_call(command, row, round_dir))
    return calls
