# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Flat-linearization plan PR-7: the before/after visualization + anomaly
callouts on jts.local.

This module owns the concerns that are genuinely PR-7's own rather than a
re-test of what an earlier PR already pins:

* **The page shell** (:mod:`jasper.web.correction_crossover_flow`) carries
  every container element ``deploy/assets/correction/js/crossover/main.js``'s
  ``els`` map reads by id (``cloud.js`` never calls ``getElementById``
  itself — it only reads properties off the ``els`` object ``main.js``
  builds and passes in) — a rename on either side is a silent breakage this
  test catches by parsing BOTH the HTML and the JS, not asserting one side
  alone (review N-4).
* **Hardware-noun discipline over PR-7's OWN authored copy** — the static
  legend/heading/provenance strings this PR wrote, as opposed to the carve-out
  disclosure strings plan PR-6b's ``crossover_v2.spatial.carve_outs_by_band``
  produces and already pins with its own
  ``test_carve_out_copy_names_no_hardware_and_no_room_furniture``
  (``tests/test_crossover_v2_cloud_pipeline.py``). Two different authors, two
  different tests — this file does not re-assert PR-6b's.

The payload plumbing (``compact_cloud_status``'s ``reference_db``/
``tolerance_db``/``provenance_note``, the new ``chart_cloud_status``
projection, ``_cloud_summary``'s session-id stamp, and the envelope's
``cloud_chart`` key) is pinned where its siblings already live:
``tests/test_correction_crossover_v2_endpoints.py`` and
``tests/test_flat_spec_ssot.py`` (the frame-consistency walk). The rendered
callout/provenance/geometry text is pinned in
``tests/js/crossover_cloud_callouts_test.mjs`` — a Python test cannot execute
the ES module, and the copy itself is server-owned, so there is nothing left
for a Python-side "renders the callout" test to check beyond what those two
already cover.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.active_speaker.crossover_v2 import journey
from jasper.web.correction_crossover_flow import render_page

# Mirrors tests/test_crossover_v2_cloud_pipeline.py's own
# _FORBIDDEN_COPY_NOUNS — deliberately re-declared rather than imported (a
# shared list between two independent test modules is a coincidence, not a
# dependency; either can extend its own without touching the other). See that
# module's comment for the full rationale: generalize by contract and
# measured evidence, never by device taxonomy, and a single session cannot
# tell a source-fixed defect from a room-fixed one either.
_FORBIDDEN_COPY_NOUNS = (
    "horn", "rim", "baffle", "cabinet", "waveguide", "dome", "tweeter",
    "woofer", "driver", "enclosure", "port", "desk", "wall", "floor",
    "ceiling", "table", "furniture",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_JS = REPO_ROOT / "deploy/assets/correction/js/crossover/chart.js"
CLOUD_JS = REPO_ROOT / "deploy/assets/correction/js/crossover/cloud.js"
MAIN_JS = REPO_ROOT / "deploy/assets/correction/js/crossover/main.js"

_GET_ELEMENT_BY_ID_RE = re.compile(r"document\.getElementById\((['\"])([^'\"]+)\1\)")


def _assert_hardware_blind(text: str, *, source: str) -> None:
    lowered = text.lower()
    for noun in _FORBIDDEN_COPY_NOUNS:
        assert not re.search(rf"\b{noun}s?\b", lowered), (source, noun)


def _is_js_comment_line(line: str) -> bool:
    """True for whole-line JS comments (mirrors
    tests/test_web_wizard_conventions.py's own ``_is_comment_line`` — kept as
    a separate, deliberately tiny local copy rather than a cross-test-module
    import, the same "coincidence, not a dependency" reasoning as
    ``_FORBIDDEN_COPY_NOUNS`` above)."""
    stripped = line.lstrip()
    return stripped.startswith(("//", "*", "/*"))


def _assert_js_shipped_copy_is_hardware_blind(path: Path) -> None:
    """The hardware-noun rule is about SHIPPED copy — what a household's
    browser actually renders — not engineering comments explaining why the
    code does what it does (e.g. this PR's own domain-bounding comments
    reference "a driver's natural top-octave rolloff" as the reason the
    y-scale is bounded to the spec's graded range, which is accurate
    engineering documentation, never sent to a household). Comment lines are
    excluded before scanning; a hardcoded fallback label on a CODE line
    still fails this check."""
    lines = path.read_text(encoding="utf-8").splitlines()
    code_only = "\n".join(line for line in lines if not _is_js_comment_line(line))
    _assert_hardware_blind(code_only, source=str(path))


def test_page_shell_carries_every_element_id_the_renderer_reads():
    """A rename on either side (the Python template or the JS ``els`` map)
    is a silent, untestable-by-JS-alone breakage — cloud.js's ``renderCloud``
    would throw on a null element the first time a real envelope arrived.

    Review N-4: parses BOTH sides rather than asserting the HTML side alone
    — the earlier version of this test would have stayed green through a
    ``main.js`` rename that orphaned an id in the HTML (or vice versa),
    because it never looked at the JS at all. This walks
    ``main.js``'s own ``document.getElementById(...)`` calls (the one place
    the id-to-property wiring for this whole page lives — ``cloud.js`` never
    calls ``getElementById`` itself, it only reads properties off the
    ``els`` object ``main.js`` builds and passes in) and asserts every
    PR-7-owned id it finds is BOTH read by the JS and rendered by the page,
    in both directions.
    """
    main_js_ids = {
        match.group(2)
        for match in _GET_ELEMENT_BY_ID_RE.finditer(MAIN_JS.read_text(encoding="utf-8"))
    }
    cloud_ids = {
        "crossover-cloud",
        "crossover-cloud-provenance",
        "crossover-cloud-chart",
        "crossover-cloud-geometry",
        "crossover-cloud-callouts",
        "crossover-cloud-pending",
        # Issue #2152: the section's own framing is set at render time from
        # what is actually on the canvas, so these three are exactly the
        # silent-drift shape this test exists for — rename one and the
        # heading silently reverts to claiming a measurement the driver-only
        # review screen does not have.
        "crossover-cloud-eyebrow",
        "crossover-cloud-title",
        "crossover-cloud-basis",
        "crossover-chart-legend-measure",
        "crossover-chart-legend-verify",
        "crossover-chart-legend-corridor",
        "crossover-chart-legend-excluded",
    }
    # Every PR-7 id this test cares about must actually be one main.js reads
    # — a typo in either this test's own list or in main.js's
    # getElementById call would otherwise go uncaught.
    missing_from_js = cloud_ids - main_js_ids
    assert not missing_from_js, (
        f"main.js does not read these ids via getElementById: {missing_from_js}"
    )

    html = render_page("jts.local").decode()
    for element_id in cloud_ids:
        assert f'id="{element_id}"' in html, element_id
    # The chart canvas and its wrapper — the room page's own precedent shape
    # (deploy/assets/correction/js/main.js's #chart / .chart-wrap).
    assert "<canvas" in html
    assert "crossover-chart-wrap" in html
    # The legend enumerates all four series the chart draws (measure, verify,
    # corridor, excluded) — a chart with an undocumented fifth color would be
    # exactly the kind of "trust me" UI this program's honesty stance argues
    # against.
    for label in (
        "Before correction", "After correction", "Spec tolerance",
        "Excluded (interference)",
    ):
        assert label in html, label
    # The section starts hidden — cloud.js's renderCloud() is what reveals
    # it once real curve data exists; a page that renders it visible by
    # default would flash an empty chart before the first envelope poll
    # resolves. The progressive-legend items (review S-5) and the
    # verify-pending caption start hidden too — renderCloud() is what shows
    # each one once its own data exists.
    cloud_section = re.search(
        r'<section id="crossover-cloud"[^>]*>', html,
    )
    assert cloud_section is not None
    assert "hidden" in cloud_section.group(0)
    for hidden_id in ("crossover-cloud-pending", "crossover-cloud-geometry"):
        element = re.search(rf'<p id="{hidden_id}"[^>]*>', html)
        assert element is not None
        assert "hidden" in element.group(0), hidden_id


def test_page_shells_own_authored_copy_is_hardware_blind():
    """The static strings THIS page renders (as opposed to the carve-out
    disclosure PR-6b's crossover_v2.spatial.carve_outs_by_band produces and
    already pins its own hardware-noun test against) must hold to the same
    rule: no speaker-part or room-furniture noun, ever."""
    html = render_page("jts.local").decode()
    _assert_hardware_blind(html, source="correction_crossover_flow.render_page")


def test_new_js_modules_are_hardware_blind():
    """Same discipline over the two new ES modules' own SHIPPED code — any
    literal strings they carry (chart.js draws from server numbers only and
    has no copy of its own; cloud.js's one literal is the verify-pending
    caption). Scoped to code lines, not comments (see
    ``_assert_js_shipped_copy_is_hardware_blind``'s own docstring): these
    modules' engineering comments legitimately discuss driver rolloff
    behavior when explaining the chart's domain-bounding math, which is
    accurate documentation, never sent to a household. A future edit that
    hardcodes a fallback label on a CODE line would still fail this check —
    exactly the device-taxonomy guess the plan forbids in shipped copy."""
    for path in (CHART_JS, CLOUD_JS):
        _assert_js_shipped_copy_is_hardware_blind(path)


MEASUREMENTS_JS = REPO_ROOT / "deploy/assets/correction/js/measurements.js"

_CLOUD_MEASURE_CONST_RE = re.compile(r"PHASE_CLOUD_MEASURE\s*=\s*['\"]([^'\"]+)['\"]")
_CLOUD_VERIFY_CONST_RE = re.compile(r"PHASE_CLOUD_VERIFY\s*=\s*['\"]([^'\"]+)['\"]")
_SERIES_KIND_RE = re.compile(r"series\.kind\s*===\s*['\"]([^'\"]+)['\"]")


def test_js_phase_slugs_pinned_to_journey_constants():
    """cloud.js's PHASE_CLOUD_MEASURE/PHASE_CLOUD_VERIFY and measurements.js's
    ``series.kind === 'entry_baseline'`` are unpinned literal copies of three
    journey.py phase slugs, with no import connecting them. Renaming a slug in
    journey.py would leave the wizard rendering nothing, silently, with both
    the Python and JS suites green — this pins the JS literals to the Python
    constants so a rename fails loudly here instead.
    """
    cloud_text = CLOUD_JS.read_text(encoding="utf-8")
    cloud_measure = _CLOUD_MEASURE_CONST_RE.search(cloud_text)
    cloud_verify = _CLOUD_VERIFY_CONST_RE.search(cloud_text)
    assert cloud_measure is not None and cloud_verify is not None
    assert cloud_measure.group(1) == journey.PHASE_CLOUD_MEASURE
    assert cloud_verify.group(1) == journey.PHASE_CLOUD_VERIFY

    # measurements.js compares series.kind against several literals (e.g.
    # 'average'); membership rather than the first match, since the intent is
    # "entry_baseline appears as a kind check", not "it's the first one".
    series_kinds = set(_SERIES_KIND_RE.findall(
        MEASUREMENTS_JS.read_text(encoding="utf-8")
    ))
    assert journey.PHASE_ENTRY_BASELINE in series_kinds
