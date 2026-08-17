#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compare two dumps of named measurement readings, before and after a change.

A measurement reading is a fact about hardware, and it lives in the repo in
three kinds of place: inside a test that pins it, inside a tolerance that
accepts a range around it, and inside prose that explains why a constant is
the value it is. **Only the first kind can ever go red.** PR #2062 (issue
#2045) is the worked example: PR #1991 legitimately moved one S0 capture's
gate, 31 of 155 compared readings moved with it, and disposing of that took
four passes because two of the three classes are invisible to every lane —

* **12 pins went red.** A lane finds these.
* **1 reading was absorbed by a tolerance** — the 1.8 kHz dip depth landed
  inside ``pytest.approx(5.19, abs=0.05)`` with **0.0041 dB to spare**, so the
  suite passed on a stale number. No lane can surface that.
* **7 prose homes restated the same facts**, three of them pinned by nothing
  at all, and one had been contradicting the very test it names.

The instrument that found classes 2 and 3 was a value-level comparator built
by hand during that diagnosis and thrown away. This is that comparator,
committed (issue #1884, rider (d)).

**It does not replace the human-executed corpus lane, and it is not CI.**
The standing ruling on #1884 is that corpus-gated tests stay laptop-local and
human-run; this sits on top of that lane. It reads two JSON files and some
declared source files, and nothing else.

Usage — dump the readings once on the base commit, once on the branch, then::

    PYTHONPATH=. .venv/bin/python scripts/compare-readings.py before.json after.json

Producing the dumps is the caller's job and is deliberately not this tool's:
which readings matter is a property of the change under test, so a measurement
PR writes a throwaway dump script that drives the shipped code paths and
serializes what it got. This tool owns the comparison and the classification.

Dump format — a JSON object mapping a reading's name to either a bare value::

    {"s0.cloud_04.floor_hz": 1777.7777777777778}

or a record that also declares a tolerance and the reading's other homes::

    {
      "nulls.dip_1800.depth_db": {
        "value": 5.144103951440755,
        "tolerance": 0.05,
        "homes": ["jasper/audio_measurement/interference_nulls.py"]
      }
    }

``tolerance`` and ``homes`` are read from the **after** dump, so one file owns
that metadata. Home paths resolve relative to the current working directory.

What it reports, in urgency order:

* **MOVED** — changed beyond its tolerance, or changed with no tolerance
  declared.
* **ABSORBED** — changed, and a declared tolerance accepted it. Printed with
  the headroom left, because that is the number that says how close the pin
  came to going red.
* **UNCHANGED / ADDED / REMOVED** — counted. A reading that exists in only one
  dump is named rather than dropped.
* **PROSE HOMES** — for every moved or absorbed reading, each declared home is
  scanned for renderings of the **before** value at 0-6 decimal places. A
  rendering that is also a rendering of the after value is skipped, so a site
  that already reads correctly is not flagged. A declared home that does not
  exist on disk is reported too.

Home hits are **candidate sites for a human to judge, not proof of drift** —
the scan matches a number, and cannot know which fact that number is stating.
This tool is advisory: a completed comparison exits 0 whatever it found. It
exits 2 only when it could not do the comparison at all.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

MOVED = "moved"
ABSORBED = "absorbed"
UNCHANGED = "unchanged"
ADDED = "added"
REMOVED = "removed"

# Renderings shorter than this are dropped before scanning: a bare "5" or
# "42" matches half a source file and would bury the real hits.
MIN_RENDERING_LEN = 3
# 1777.7777777777778 renders as "1778" at 0 places and "1777.8" at 1. Both
# shapes are in the tree today, so the span has to cover at least that, and
# widening it to 6 costs one more regex per home line.
MAX_RENDERING_DECIMALS = 6

_NUMERIC_RENDERING = re.compile(r"^-?\d+(\.\d+)?$")


class DumpError(Exception):
    """A dump file could not be read as a reading dump."""


@dataclass(frozen=True)
class Reading:
    """One named value, with the metadata the after-dump may declare for it."""

    name: str
    value: Any
    tolerance: float | None = None
    homes: tuple[str, ...] = ()


@dataclass(frozen=True)
class HomeHit:
    """A declared home still carrying a rendering of the before value."""

    path: str
    line_no: int
    rendering: str
    line: str


@dataclass(frozen=True)
class Comparison:
    """One reading's verdict."""

    name: str
    status: str
    before: Any = None
    after: Any = None
    tolerance: float | None = None
    delta: float | None = None
    home_hits: tuple[HomeHit, ...] = ()
    missing_homes: tuple[str, ...] = ()

    @property
    def headroom(self) -> float | None:
        """How much tolerance the move left, for an absorbed reading."""

        if self.tolerance is None or self.delta is None:
            return None
        return self.tolerance - abs(self.delta)


def _is_number(value: Any) -> bool:
    # `bool` is an `int` subclass and is not a reading; comparing True to 1.0
    # numerically would report a type flip as "unchanged".
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_dump(payload: Any, source: str) -> dict[str, Reading]:
    """Turn a decoded dump into readings, or raise :class:`DumpError`."""

    if not isinstance(payload, dict):
        raise DumpError(f"{source}: top level must be an object, got {type(payload).__name__}")
    readings: dict[str, Reading] = {}
    for name, record in payload.items():
        if isinstance(record, dict):
            if "value" not in record:
                raise DumpError(f"{source}: reading {name!r} has no 'value'")
            value = record["value"]
            tolerance = record.get("tolerance")
            if tolerance is not None and not _is_number(tolerance):
                raise DumpError(f"{source}: reading {name!r} has a non-numeric 'tolerance'")
            homes_raw = record.get("homes") or ()
            if not isinstance(homes_raw, (list, tuple)) or not all(
                isinstance(home, str) for home in homes_raw
            ):
                raise DumpError(f"{source}: reading {name!r} has a non-string-list 'homes'")
            homes = tuple(homes_raw)
        else:
            value, tolerance, homes = record, None, ()
        readings[name] = Reading(
            name=name,
            value=value,
            tolerance=float(tolerance) if tolerance is not None else None,
            homes=homes,
        )
    return readings


def load_dump(path: Path) -> dict[str, Reading]:
    """Read a dump file, or raise :class:`DumpError` naming the file."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DumpError(f"{path}: {exc.strerror or exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DumpError(f"{path}: not valid JSON ({exc})") from exc
    return parse_dump(payload, str(path))


def renderings(value: Any) -> list[str]:
    """Every way this value plausibly appears in prose, longest first."""

    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif _is_number(value):
        number = float(value)
        if isinstance(value, int):
            out.append(str(value))
        else:
            out.append(repr(value))
            for places in range(MAX_RENDERING_DECIMALS + 1):
                out.append(f"{number:.{places}f}")
            if number.is_integer():
                out.append(str(int(number)))
    unique = list(dict.fromkeys(out))
    kept = [text for text in unique if len(text) >= MIN_RENDERING_LEN]
    # Longest first so a line carrying "1777.7778" is reported at that
    # precision rather than as the shorter "1778" that also matches it.
    return sorted(kept, key=len, reverse=True)


def _boundary_pattern(rendering: str) -> re.Pattern[str]:
    if _NUMERIC_RENDERING.match(rendering):
        # "5.14" must not match inside "5.144" or "15.14", but must still
        # match at the end of a sentence ("... 5.14.").
        return re.compile(r"(?<![\d.])" + re.escape(rendering) + r"(?!\d)(?!\.\d)")
    return re.compile(r"(?<!\w)" + re.escape(rendering) + r"(?!\w)")


def scan_home(path: Path, candidates: Sequence[str]) -> list[HomeHit]:
    """Find candidate renderings in one file, at most one hit per line."""

    patterns = [(text, _boundary_pattern(text)) for text in candidates]
    hits: list[HomeHit] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for line_no, line in enumerate(text.splitlines(), start=1):
        for rendering, pattern in patterns:
            if pattern.search(line):
                hits.append(
                    HomeHit(
                        path=str(path),
                        line_no=line_no,
                        rendering=rendering,
                        line=line.strip(),
                    )
                )
                break
    return hits


def _scan_homes(
    homes: Sequence[str], before: Any, after: Any
) -> tuple[tuple[HomeHit, ...], tuple[str, ...]]:
    superseded = [text for text in renderings(before) if text not in set(renderings(after))]
    hits: list[HomeHit] = []
    missing: list[str] = []
    for home in homes:
        path = Path(home)
        if not path.is_file():
            missing.append(home)
            continue
        if superseded:
            hits.extend(scan_home(path, superseded))
    return tuple(hits), tuple(missing)


def compare(
    before: dict[str, Reading], after: dict[str, Reading]
) -> list[Comparison]:
    """Classify every reading in either dump. Never drops one."""

    out: list[Comparison] = []
    for name in sorted(set(before) | set(after)):
        was, now = before.get(name), after.get(name)
        if now is None:
            assert was is not None
            out.append(Comparison(name=name, status=REMOVED, before=was.value))
            continue
        if was is None:
            out.append(Comparison(name=name, status=ADDED, after=now.value))
            continue
        numeric = _is_number(was.value) and _is_number(now.value)
        delta = float(now.value) - float(was.value) if numeric else None
        if was.value == now.value:
            out.append(
                Comparison(
                    name=name,
                    status=UNCHANGED,
                    before=was.value,
                    after=now.value,
                    tolerance=now.tolerance,
                    delta=delta,
                )
            )
            continue
        absorbed = (
            delta is not None
            and now.tolerance is not None
            and abs(delta) <= now.tolerance
        )
        hits, missing = _scan_homes(now.homes, was.value, now.value)
        out.append(
            Comparison(
                name=name,
                status=ABSORBED if absorbed else MOVED,
                before=was.value,
                after=now.value,
                tolerance=now.tolerance,
                delta=delta,
                home_hits=hits,
                missing_homes=missing,
            )
        )
    return out


def _value_lines(item: Comparison) -> list[str]:
    lines = [f"      before {item.before!r}", f"      after  {item.after!r}"]
    if item.delta is None:
        lines.append("      delta  not numeric")
        return lines
    detail = f"      delta  {item.delta!r}"
    if item.tolerance is None:
        detail += "   (no tolerance declared)"
    else:
        detail += f"   tolerance {item.tolerance!r}"
        headroom = item.headroom
        if headroom is not None and item.status == ABSORBED:
            detail += f"   headroom {headroom!r}"
    lines.append(detail)
    return lines


def render_report(comparisons: Sequence[Comparison]) -> str:
    """The whole finding as text. Every section prints, including empty ones."""

    by_status: dict[str, list[Comparison]] = {
        status: [item for item in comparisons if item.status == status]
        for status in (MOVED, ABSORBED, UNCHANGED, ADDED, REMOVED)
    }
    lines = [f"reading comparison: {len(comparisons)} reading(s) compared", ""]

    lines.append(
        f"MOVED ({len(by_status[MOVED])}) "
        "- beyond tolerance, or no tolerance declared"
    )
    for item in by_status[MOVED]:
        lines.append(f"  {item.name}")
        lines.extend(_value_lines(item))
    lines.append("")

    lines.append(
        f"ABSORBED ({len(by_status[ABSORBED])}) "
        "- moved, and a declared tolerance accepted it"
    )
    for item in by_status[ABSORBED]:
        lines.append(f"  {item.name}")
        lines.extend(_value_lines(item))
    lines.append("")

    lines.append(f"UNCHANGED ({len(by_status[UNCHANGED])})")
    lines.append(f"ADDED ({len(by_status[ADDED])})")
    for item in by_status[ADDED]:
        lines.append(f"  {item.name}   after {item.after!r}")
    lines.append(f"REMOVED ({len(by_status[REMOVED])})")
    for item in by_status[REMOVED]:
        lines.append(f"  {item.name}   before {item.before!r}")
    lines.append("")

    hit_owners = [item for item in comparisons if item.home_hits]
    hit_count = sum(len(item.home_hits) for item in hit_owners)
    lines.append(
        f"PROSE HOMES ({hit_count}) - declared homes still carrying a "
        "superseded value, across "
        f"{len(hit_owners)} reading(s). Candidate sites, not proof."
    )
    for item in hit_owners:
        lines.append(f"  {item.name}")
        for hit in item.home_hits:
            lines.append(f"    {hit.path}:{hit.line_no}  {hit.rendering!r}")
            lines.append(f"      {hit.line}")
    lines.append("")

    missing_owners = [item for item in comparisons if item.missing_homes]
    missing_count = sum(len(item.missing_homes) for item in missing_owners)
    lines.append(f"HOMES NOT FOUND ({missing_count})")
    for item in missing_owners:
        for home in item.missing_homes:
            lines.append(f"  {item.name}   {home}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compare-readings.py",
        description=(
            "Compare two dumps of named measurement readings and report what "
            "moved, what a tolerance absorbed, and which declared prose homes "
            "still carry the superseded value."
        ),
    )
    parser.add_argument("before", type=Path, help="JSON dump taken on the base commit")
    parser.add_argument("after", type=Path, help="JSON dump taken on the branch")
    args = parser.parse_args(argv)

    try:
        before = load_dump(args.before)
        after = load_dump(args.after)
    except DumpError as exc:
        print(f"compare-readings: {exc}", file=sys.stderr)
        return 2

    sys.stdout.write(render_report(compare(before, after)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
