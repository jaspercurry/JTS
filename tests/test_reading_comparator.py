# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``scripts/compare-readings.py`` graded on the three #2062 drift classes.

PR #2062 (issue #2045) is the incident the tool was built for, and it named
its own three classes. The demonstration below is one synthetic case per
class, wired so the correct answer is known before the tool runs:

* a **pinned reading that moved beyond its tolerance** - the class a lane
  already catches (12 tests went red);
* a **reading a tolerance absorbed** - the 1.8 kHz dip depth, which landed
  inside ``approx(5.19, abs=0.05)`` with 0.0041 dB to spare and so stayed
  green on a stale number;
* a **prose home** restating a value that no longer matches its code home -
  seven such homes drifted, three pinned by nothing at all.

The fixtures use the incident's own figures because they exercise the
renderings that matter: 1777.8 Hz is quoted in prose as "1778", and 5.1441 dB
is quoted as "5.14".

Every sentence the script's module docstring states about its behaviour is
pinned here.
"""

from __future__ import annotations

import importlib.util
import json
import math
import pathlib
import sys
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_comparator():
    """``scripts/compare-readings.py`` as a module.

    It is a script rather than a package module (laptop-side, never shipped to
    the Pi) and its filename is not an identifier, so it is loaded by path.
    """

    name = "_compare_readings"
    if name in sys.modules:
        return sys.modules[name]
    path = REPO_ROOT / "scripts" / "compare-readings.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: the module defines dataclasses, and
    # `dataclasses` resolves each one's own module out of `sys.modules`.
    sys.modules[name] = module
    loaded = False
    try:
        spec.loader.exec_module(module)
        loaded = True
    finally:
        if not loaded:
            del sys.modules[name]
    return module


comparator = _load_comparator()


# --- The incident's own numbers -------------------------------------------
#
# Row 13 of PR #2062's disposition table: the dip depth moved under a
# tolerance that accepted it, and its centre moved with it.
DIP_DEPTH_BEFORE = 5.19
DIP_DEPTH_AFTER = 5.144103951440755
DIP_DEPTH_TOLERANCE = 0.05
# The gate the #1790 artifact used to produce, against the search-span bound
# nine of ten positions gate to.
FLOOR_BEFORE = 1777.7777777777778
FLOOR_AFTER = 142.857


def _dump(tmp_path: pathlib.Path, name: str, payload: dict[str, Any]) -> pathlib.Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _by_name(comparisons) -> dict[str, Any]:
    return {item.name: item for item in comparisons}


def _compare(before: dict[str, Any], after: dict[str, Any]):
    return comparator.compare(
        comparator.parse_dump(before, "before"),
        comparator.parse_dump(after, "after"),
    )


# --- Class 1: a pinned reading moved beyond tolerance ----------------------


def test_a_reading_that_moved_beyond_its_tolerance_is_reported_as_moved() -> None:
    """The class a lane already catches, restated at value level."""

    results = _by_name(
        _compare(
            {"s0.cloud_04.floor_hz": FLOOR_BEFORE},
            {
                "s0.cloud_04.floor_hz": {
                    "value": FLOOR_AFTER,
                    "tolerance": 0.05,
                }
            },
        )
    )
    moved = results["s0.cloud_04.floor_hz"]
    assert moved.status == comparator.MOVED
    assert moved.before == FLOOR_BEFORE
    assert moved.after == FLOOR_AFTER
    assert moved.delta == pytest.approx(FLOOR_AFTER - FLOOR_BEFORE, abs=1e-12)


def test_a_move_with_no_declared_tolerance_is_moved_not_absorbed() -> None:
    """No tolerance means nothing can absorb it - a bare value is not a pass."""

    results = _by_name(
        _compare({"n_excluded": 2949}, {"n_excluded": 2938}),
    )
    assert results["n_excluded"].status == comparator.MOVED
    assert results["n_excluded"].tolerance is None


def test_an_identical_reading_is_unchanged_and_a_non_numeric_flip_is_moved() -> None:
    """Exact equality at full precision, and strings compare too.

    ``floor_source`` is a string reading in the incident; it flipped from
    ``measured_reflection`` to ``search_span_bound``.
    """

    results = _by_name(
        _compare(
            {
                "floor_source": "measured_reflection",
                "tau_us": 298.777,
            },
            {
                # A tolerance cannot absorb a change with no numeric delta,
                # so a declared one must not silently accept this.
                "floor_source": {
                    "value": "search_span_bound",
                    "tolerance": 1000.0,
                },
                "tau_us": 298.777,
            },
        )
    )
    assert results["tau_us"].status == comparator.UNCHANGED
    flipped = results["floor_source"]
    assert flipped.status == comparator.MOVED
    assert flipped.delta is None


def test_a_bool_flipping_to_a_number_is_a_change_not_a_silence() -> None:
    """`True == 1.0` in Python, so plain equality would call this unchanged.

    A type flip is a real change, and this tool's thesis is that silence is
    the enemy. `_same` is what enforces it - excluding bool from `_is_number`
    only nulls the delta, and never touched the status.
    """

    assert True == 1.0  # noqa: E712 - the premise, stated rather than implied

    for before, after in ((True, 1.0), (1.0, True), (False, 0), (0, False)):
        results = _by_name(_compare({"flag": before}, {"flag": after}))
        assert results["flag"].status == comparator.MOVED, (before, after)
        assert results["flag"].delta is None, "a bool has no numeric delta"

    # A bool that did not flip is still unchanged.
    assert (
        _by_name(_compare({"flag": True}, {"flag": True}))["flag"].status
        == comparator.UNCHANGED
    )


def test_an_int_that_becomes_a_float_is_not_a_flip() -> None:
    """5 and 5.0 are the same reading.

    Deliberately NOT reported: a dump script that starts calling `float()` is
    not a measurement change, and flagging it would be the noise this tool
    spends its credibility on avoiding.
    """

    results = _by_name(_compare({"n_bins": 5}, {"n_bins": 5.0}))
    assert results["n_bins"].status == comparator.UNCHANGED


def test_a_float_that_differs_below_display_precision_still_moves() -> None:
    """Comparison is at full precision, not at whatever a report would print.

    One ULP apart: both print as 3.0878 at any precision prose would use.
    """

    nudged = math.nextafter(3.0878, math.inf)
    assert f"{nudged:.6f}" == f"{3.0878:.6f}"
    results = _by_name(_compare({"sigma_db": 3.0878}, {"sigma_db": nudged}))
    assert results["sigma_db"].status == comparator.MOVED


# --- Class 2: a tolerance absorbed the drift ------------------------------


def test_a_tolerance_absorbed_move_is_reported_with_its_headroom() -> None:
    """The #2062 failure was silence here, so absorbed is a reported class.

    0.0041 dB of headroom is the whole finding: the pin passed, and the next
    honest re-read would have tipped it red as a phantom regression.
    """

    results = _by_name(
        _compare(
            {"nulls.dip_1800.depth_db": DIP_DEPTH_BEFORE},
            {
                "nulls.dip_1800.depth_db": {
                    "value": DIP_DEPTH_AFTER,
                    "tolerance": DIP_DEPTH_TOLERANCE,
                }
            },
        )
    )
    absorbed = results["nulls.dip_1800.depth_db"]
    assert absorbed.status == comparator.ABSORBED
    assert absorbed.delta == pytest.approx(-0.045896048559245, abs=1e-12)
    assert absorbed.headroom == pytest.approx(0.0041039514407, abs=1e-9)


def test_the_tolerance_comparison_is_inclusive_like_pytest_approx() -> None:
    """A move equal to the tolerance is absorbed, not moved.

    Values chosen to be exactly representable, because at the boundary it is
    float arithmetic that decides - ``1.0 -> 1.05`` against ``abs=0.05``
    gives a delta of 0.050000000000000044 and moves. ``pytest.approx`` has
    the same property, which is the point: this agrees with the pins it is
    reasoning about rather than with the decimal they appear to say.
    """

    results = _by_name(
        _compare({"x": 1.0}, {"x": {"value": 1.5, "tolerance": 0.5}}),
    )
    assert results["x"].status == comparator.ABSORBED
    assert results["x"].headroom == 0.0

    edge = _by_name(
        _compare({"x": 1.0}, {"x": {"value": 1.05, "tolerance": 0.05}}),
    )
    assert edge["x"].status == comparator.MOVED


def test_the_absorbed_reading_is_visible_in_the_report_not_only_the_data() -> None:
    """A finding the report omits is the same silence the tool exists to end."""

    report = comparator.render_report(
        _compare(
            {"nulls.dip_1800.depth_db": DIP_DEPTH_BEFORE},
            {
                "nulls.dip_1800.depth_db": {
                    "value": DIP_DEPTH_AFTER,
                    "tolerance": DIP_DEPTH_TOLERANCE,
                }
            },
        )
    )
    assert "ABSORBED (1)" in report
    assert "nulls.dip_1800.depth_db" in report
    assert "headroom" in report
    # Full precision, not a rounded display value.
    assert repr(DIP_DEPTH_AFTER) in report


def test_every_section_prints_even_when_it_is_empty() -> None:
    """The reader must be able to see that the tool looked and found nothing.

    Two reports, so every section is asserted at zero in one of them: an
    unchanged-only run empties MOVED, and a moved-only run empties the rest.
    """

    unchanged_only = comparator.render_report(_compare({"x": 1.0}, {"x": 1.0}))
    assert "MOVED (0)" in unchanged_only

    moved_only = comparator.render_report(_compare({"x": 1.0}, {"x": 2.0}))
    for heading in (
        "ABSORBED (0)",
        "UNCHANGED (0)",
        "ADDED (0)",
        "REMOVED (0)",
        "PROSE HOMES (0)",
        "HOMES NOT FOUND (0)",
        "HOMES NOT SCANNED (0)",
    ):
        assert heading in moved_only


# --- Class 3: a prose home that no longer matches its code home -----------


def test_a_prose_home_still_carrying_the_superseded_value_is_reported(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The class nothing catches: a constant's rationale quoting a stale number.

    Both homes here restate the same fact at DIFFERENT precision - a docstring
    at two decimals and a table at four - which is why the scan renders the
    before value at several decimal places rather than one.
    """

    module = tmp_path / "interference_nulls.py"
    module.write_text(
        '"""The dip is refused by depth: the ladder fills it from 5.19 dB."""\n'
        "DEPTH_CEILING_MARGIN_DB = 0.98\n",
        encoding="utf-8",
    )
    table = tmp_path / "notes.txt"
    table.write_text(
        "| rung | depth |\n| 1.8 kHz | 5.1900 |\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    results = _by_name(
        _compare(
            {"nulls.dip_1800.depth_db": DIP_DEPTH_BEFORE},
            {
                "nulls.dip_1800.depth_db": {
                    "value": DIP_DEPTH_AFTER,
                    "tolerance": DIP_DEPTH_TOLERANCE,
                    "homes": ["interference_nulls.py", "notes.txt"],
                }
            },
        )
    )
    hits = results["nulls.dip_1800.depth_db"].home_hits
    assert {(hit.path, hit.line_no, hit.rendering) for hit in hits} == {
        ("interference_nulls.py", 1, "5.19"),
        ("notes.txt", 2, "5.1900"),
    }
    report = comparator.render_report(
        _compare(
            {"nulls.dip_1800.depth_db": DIP_DEPTH_BEFORE},
            {
                "nulls.dip_1800.depth_db": {
                    "value": DIP_DEPTH_AFTER,
                    "tolerance": DIP_DEPTH_TOLERANCE,
                    "homes": ["interference_nulls.py", "notes.txt"],
                }
            },
        )
    )
    assert "PROSE HOMES (2)" in report
    assert "interference_nulls.py:1" in report


def test_the_prose_scan_runs_on_absorbed_readings_too(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classes 2 and 3 compounded is exactly the #2062 case.

    The depth above was absorbed by its tolerance AND restated in prose. If
    the scan only ran for MOVED readings, the incident's own sharpest finding
    would still be invisible.
    """

    home = tmp_path / "rationale.py"
    home.write_text("# refused at 5.19 dB\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    results = _by_name(
        _compare(
            {"depth": DIP_DEPTH_BEFORE},
            {
                "depth": {
                    "value": DIP_DEPTH_AFTER,
                    "tolerance": DIP_DEPTH_TOLERANCE,
                    "homes": ["rationale.py"],
                }
            },
        )
    )
    assert results["depth"].status == comparator.ABSORBED
    assert [hit.rendering for hit in results["depth"].home_hits] == ["5.19"]


def test_a_prose_home_that_already_reads_correctly_is_not_flagged(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rendering that is also a rendering of the after value is skipped.

    The mechanism, so it is not mistaken for luck: both values round to
    "1778" at 0 places, so that rendering is dropped before the scan, while
    "1777.8" is not a rendering of the after value and would still be found.
    """

    home = tmp_path / "doc.txt"
    home.write_text("the group floor is 1778 Hz\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    results = _by_name(
        _compare(
            {"floor_hz": FLOOR_BEFORE},
            {"floor_hz": {"value": 1777.9, "homes": ["doc.txt"]}},
        )
    )
    assert results["floor_hz"].status == comparator.MOVED
    assert results["floor_hz"].home_hits == ()
    assert results["floor_hz"].unscanned_homes == (), (
        "already-correct is not a hand check"
    )


@pytest.mark.parametrize(
    "before, after",
    [
        (1778, 1777.7777777777778),
        (100, 100.4),
        (1778, 1778.0001),
    ],
)
def test_an_already_correct_home_is_not_demoted_into_the_hand_check(
    before: Any,
    after: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to search for and nothing to find are different verdicts.

    An int before value has exactly one rendering, and when the after value
    rounds to it the home already reads correctly - `1778` IS right for
    1777.7777777777778. Folding that into the hand-check bucket would demand
    a human open a file with nothing to find, and would print a reason that
    is false: "1778" is four characters, not too short to search for.
    """

    home = tmp_path / "home.py"
    home.write_text(f"FLOOR_HZ = {before}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert comparator.renderings(before), "premise: there IS a rendering"
    result = _by_name(
        _compare({"floor": before}, {"floor": {"value": after, "homes": ["home.py"]}}),
    )["floor"]
    assert result.status == comparator.MOVED
    assert result.home_hits == ()
    assert result.unscanned_homes == ()
    assert result.missing_homes == ()


def test_a_home_the_scan_could_not_look_inside_is_reported_not_silent(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2062 class 3, reintroduced by the instrument built to end it.

    A rung count of 12 has no rendering worth searching for, so the file is
    never opened. Printing that as PROSE HOMES (0) would say "scanned, clean"
    about a file carrying the superseded value on the same line as its
    rationale - the exact silence this tool exists to break.
    """

    home = tmp_path / "home.py"
    home.write_text(
        "# The ladder needs 12 rungs, so the count is 12.\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    comparisons = _compare(
        {"nulls.n_rungs": 12},
        {"nulls.n_rungs": {"value": 14, "homes": ["home.py"]}},
    )
    rung = _by_name(comparisons)["nulls.n_rungs"]
    assert comparator.renderings(12) == [], "premise: nothing to search for"
    assert rung.home_hits == ()
    assert rung.missing_homes == (), "the file is on disk; it was not opened"
    assert rung.unscanned_homes == ("home.py",)

    report = comparator.render_report(comparisons)
    assert "HOMES NOT SCANNED (1)" in report
    assert "home.py" in report
    # The before value rides along, so the hand check knows what to look for.
    assert "before 12" in report


@pytest.mark.parametrize(
    "before, after",
    [
        ("ab", "cd"),
        (None, 5.0),
        ([1, 2], [1, 3]),
        ({"a": 1}, {"a": 2}),
    ],
)
def test_any_reading_with_nothing_to_search_for_lands_in_the_hand_check(
    before: Any,
    after: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same route as a short int, for the other shapes JSON can carry.

    Deliberately NOT a claim about which shapes those are. A `null` is
    natural in a measurement dump - a reading not measured this run - and a
    census of the rest would be an unpinned completeness claim in a tool
    whose subject is exactly that kind of prose.

    The before dump uses the record form throughout because a bare dict is
    read as a record: `{"x": {"a": 1}}` is a record missing its 'value', not
    a dict-valued reading. `{"x": {"value": {"a": 1}}}` is how you say that.
    """

    home = tmp_path / "home.py"
    home.write_text("MODE = 'ab'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert comparator.renderings(before) == []
    comparisons = _compare(
        {"x": {"value": before}},
        {"x": {"value": after, "homes": ["home.py"]}},
    )
    assert _by_name(comparisons)["x"].unscanned_homes == ("home.py",)

    report = comparator.render_report(comparisons)
    assert "HOMES NOT SCANNED (1)" in report
    # The printed reason must fit every cause. A JSON `null` is not "under 3
    # characters", and a reason that is false on the line a human acts on is
    # worse than no reason.
    assert "characters" not in report, report


def test_a_float_never_lands_in_the_hand_check_bucket() -> None:
    """Its `repr` round-trips exactly, so a float always has something to
    search for - however small, however round."""

    for value in (0.0, -0.0, 5e-324, 1e-300, 1.797e308, 4e-07, -1e-9, 1778.0):
        assert comparator.renderings(value) != [], value


# --- Renderings that state nothing must not flood the report ---------------


def test_a_rendering_that_rounded_away_its_last_digit_is_dropped() -> None:
    """0.029 at one decimal place is "0.0" - three characters, no information.

    This band is where the corpus lives: #2062 quotes rung deltas of -0.029
    and -0.004, and its own headroom is 0.0041 dB. Length alone does not
    catch it, and neither filter subsumes the other - "5" keeps a significant
    digit and only the length rule rejects it.
    """

    assert f"{0.029:.1f}" == "0.0", "premise: the collapse is real"
    rendered = comparator.renderings(0.029)
    assert "0.0" not in rendered
    assert "0.00" not in rendered
    assert "0.03" in rendered
    assert "0.029" in rendered

    # A genuine zero reading keeps its zeros - there is nothing to lose.
    assert "0.0" in comparator.renderings(0.0)
    # Negative zero collapses the same way.
    assert "-0.0" not in comparator.renderings(-0.004)
    assert "-0.004" in comparator.renderings(-0.004)
    # And the length rule still owns its own case.
    assert "5" not in comparator.renderings(5.19)


def test_the_collapsed_rendering_does_not_bury_the_real_hit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: one hit that states the reading, not 36 that state nothing."""

    home = tmp_path / "interference_nulls.py"
    home.write_text(
        "def f(r):\n"
        "    if r <= 0.0:\n"
        "        return 0.0\n"
        "    return 0.0\n"
        "# the rung delta is 0.029 dB\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    results = _by_name(
        _compare(
            {"rung.delta_db": 0.029},
            {"rung.delta_db": {"value": 0.5, "homes": ["interference_nulls.py"]}},
        )
    )
    assert [
        (hit.line_no, hit.rendering) for hit in results["rung.delta_db"].home_hits
    ] == [(5, "0.029")]


def test_a_declared_home_that_does_not_exist_is_reported(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home that moved away is drift too, and must not read as "clean"."""

    monkeypatch.chdir(tmp_path)
    results = _by_name(
        _compare(
            {"x": 1.0},
            {"x": {"value": 2.0, "homes": ["gone.py"]}},
        )
    )
    assert results["x"].missing_homes == ("gone.py",)
    report = comparator.render_report(
        _compare({"x": 1.0}, {"x": {"value": 2.0, "homes": ["gone.py"]}})
    )
    assert "HOMES NOT FOUND (1)" in report
    assert "gone.py" in report


def test_homes_resolve_relative_to_the_working_directory(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documented behaviour: a repo-relative home works from the repo root."""

    nested = tmp_path / "jasper"
    nested.mkdir()
    (nested / "thing.py").write_text("VALUE = 5.19\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    found = _by_name(
        _compare(
            {"x": DIP_DEPTH_BEFORE},
            {"x": {"value": DIP_DEPTH_AFTER, "homes": ["jasper/thing.py"]}},
        )
    )
    assert [hit.line_no for hit in found["x"].home_hits] == [1]

    monkeypatch.chdir(nested)
    missed = _by_name(
        _compare(
            {"x": DIP_DEPTH_BEFORE},
            {"x": {"value": DIP_DEPTH_AFTER, "homes": ["jasper/thing.py"]}},
        )
    )
    assert missed["x"].missing_homes == ("jasper/thing.py",)


# --- Rendering and matching rules -----------------------------------------


def test_renderings_cover_the_decimal_places_this_repo_quotes() -> None:
    """The two shapes this floor is actually written in must both match.

    Not hypothetical: on `main` the S0 floor is written "1778" in
    `jasper/audio_measurement/gating.py` and "1777.8" in
    `jasper/active_speaker/crossover_v2_flow.py`. Full precision too, since a
    test pin carries the repr. How MANY files carry each shape is deliberately
    not stated - it would be a second unpinned copy of a count, in a tool
    whose whole subject is that kind of drift.
    """

    rendered = comparator.renderings(FLOOR_BEFORE)
    assert "1778" in rendered
    assert "1777.8" in rendered
    assert repr(FLOOR_BEFORE) in rendered
    # Longest first, so a site is reported at its most specific rendering.
    assert rendered == sorted(rendered, key=len, reverse=True)


def test_renderings_drop_the_short_ones_that_would_match_everything() -> None:
    """A bare "5" would hit half a source file and bury the real findings."""

    assert "5" not in comparator.renderings(5.19)
    assert all(len(text) >= 3 for text in comparator.renderings(5.19))
    assert comparator.renderings(True) == []


def test_a_numeric_rendering_does_not_match_inside_a_longer_number(
    tmp_path: pathlib.Path
) -> None:
    """"5.14" must not match "5.144" or "15.14", but must match "5.14."."""

    home = tmp_path / "prose.txt"
    home.write_text(
        "a: 5.144103951440755\n"
        "b: 15.14\n"
        "c: the refusal is 5.14.\n"
        "d: 5.14 dB\n",
        encoding="utf-8",
    )
    hits = comparator.scan_home(home, ["5.14"])
    assert [hit.line_no for hit in hits] == [3, 4]


def test_one_hit_per_line_at_the_most_specific_rendering(
    tmp_path: pathlib.Path
) -> None:
    """One site is one finding, not one per rendering that happens to match.

    Line 2 is the case that discriminates: a table row quoting the same
    reading rounded AND exact, where both "1778" and "1777.7778" match.
    """

    home = tmp_path / "prose.txt"
    home.write_text(
        "the floor is 1777.7778 Hz\n| 1778 | 1777.7778 |\n",
        encoding="utf-8",
    )
    hits = comparator.scan_home(home, comparator.renderings(FLOOR_BEFORE))
    assert [(hit.line_no, hit.rendering) for hit in hits] == [
        (1, "1777.7778"),
        (2, "1777.7778"),
    ]


def test_a_string_rendering_matches_on_word_boundaries(
    tmp_path: pathlib.Path
) -> None:
    home = tmp_path / "prose.txt"
    home.write_text(
        "floor_source=measured_reflection\n"
        "floor_source=measured_reflection_v2\n",
        encoding="utf-8",
    )
    hits = comparator.scan_home(home, ["measured_reflection"])
    assert [hit.line_no for hit in hits] == [1]


# --- Nothing is dropped ----------------------------------------------------


def test_a_reading_present_in_only_one_dump_is_named_not_dropped() -> None:
    """Silently dropping a reading is the same failure as absorbing it."""

    results = _by_name(_compare({"gone": 1.0}, {"fresh": 2.0}))
    assert results["gone"].status == comparator.REMOVED
    assert results["gone"].before == 1.0
    assert results["fresh"].status == comparator.ADDED
    assert results["fresh"].after == 2.0
    report = comparator.render_report(_compare({"gone": 1.0}, {"fresh": 2.0}))
    assert "ADDED (1)" in report
    assert "REMOVED (1)" in report
    assert "gone" in report and "fresh" in report


def test_metadata_is_read_from_the_after_dump() -> None:
    """One file owns tolerance and homes, so the two dumps cannot disagree."""

    results = _by_name(
        _compare(
            {"x": {"value": 1.0, "tolerance": 10.0, "homes": ["stale.py"]}},
            {"x": {"value": 2.0}},
        )
    )
    # The before dump's tolerance of 10.0 would have absorbed this move.
    assert results["x"].status == comparator.MOVED
    assert results["x"].tolerance is None
    assert results["x"].missing_homes == ()


# --- CLI contract ----------------------------------------------------------


def test_a_completed_comparison_exits_zero_whatever_it_found(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Advisory by design: it reports, it does not gate."""

    before = _dump(tmp_path, "before.json", {"x": 1.0})
    after = _dump(tmp_path, "after.json", {"x": 2.0})
    assert comparator.main([str(before), str(after)]) == 0
    out = capsys.readouterr().out
    assert "MOVED (1)" in out


def test_a_dump_it_cannot_read_exits_two(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """"I could not compare" must never read as "nothing moved"."""

    good = _dump(tmp_path, "good.json", {"x": 1.0})
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    assert comparator.main([str(good), str(broken)]) == 2
    assert "not valid JSON" in capsys.readouterr().err

    assert comparator.main([str(good), str(tmp_path / "absent.json")]) == 2
    assert "absent.json" in capsys.readouterr().err


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ([1, 2], "top level must be an object"),
        ({"x": {"tolerance": 0.1}}, "has no 'value'"),
        ({"x": {"value": 1.0, "tolerance": "wide"}}, "non-numeric 'tolerance'"),
        ({"x": {"value": 1.0, "homes": "one.py"}}, "non-string-list 'homes'"),
    ],
)
def test_a_malformed_dump_is_refused_with_the_reading_named(
    payload: Any, fragment: str
) -> None:
    with pytest.raises(comparator.DumpError) as caught:
        comparator.parse_dump(payload, "dump.json")
    assert fragment in str(caught.value)
