# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Flat-linearization plan PR-7: the before/after visualization + anomaly
callouts on jts.local.

This module owns the concerns that are genuinely PR-7's own rather than a
re-test of what an earlier PR already pins:

* **The page shell** (:mod:`jasper.web.correction_crossover_flow`) carries the
  container elements ``deploy/assets/correction/js/crossover/main.js``'s
  ``els`` map and ``cloud.js``'s ``renderCloud`` read by id — a rename on
  either side is a silent breakage this test catches.
* **Hardware-noun discipline over PR-7's OWN authored copy** — the static
  legend/heading/provenance strings this PR wrote, as opposed to the carve-out
  disclosure strings plan PR-6b's ``crossover_v2_flow.carve_outs_by_band``
  produces and already pins with its own
  ``test_carve_out_copy_names_no_hardware_and_no_room_furniture``
  (``tests/test_crossover_v2_cloud_pipeline.py``). Two different authors, two
  different tests — this file does not re-assert PR-6b's.

The payload plumbing (``_compact_cloud_status``'s ``reference_db``/
``tolerance_db``/``provenance_note``, the new ``_chart_cloud_status``
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


def _assert_hardware_blind(text: str, *, source: str) -> None:
    lowered = text.lower()
    for noun in _FORBIDDEN_COPY_NOUNS:
        assert not re.search(rf"\b{noun}s?\b", lowered), (source, noun)


def test_page_shell_carries_every_element_id_the_renderer_reads():
    """A rename on either side (the Python template or the JS ``els`` map)
    is a silent, untestable-by-JS-alone breakage — cloud.js's ``renderCloud``
    would throw on a null element the first time a real envelope arrived.
    Pins the exact ids main.js's ``els`` map + cloud.js read."""
    html = render_page("jts.local").decode()
    for element_id in (
        "crossover-cloud",
        "crossover-cloud-provenance",
        "crossover-cloud-chart",
        "crossover-cloud-geometry",
        "crossover-cloud-callouts",
    ):
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
    # resolves.
    cloud_section = re.search(
        r'<section id="crossover-cloud"[^>]*>', html,
    )
    assert cloud_section is not None
    assert "hidden" in cloud_section.group(0)


def test_page_shells_own_authored_copy_is_hardware_blind():
    """The static strings THIS page renders (as opposed to the carve-out
    disclosure PR-6b's crossover_v2_flow.carve_outs_by_band produces and
    already pins its own hardware-noun test against) must hold to the same
    rule: no speaker-part or room-furniture noun, ever."""
    html = render_page("jts.local").decode()
    _assert_hardware_blind(html, source="correction_crossover_flow.render_page")


def test_new_js_modules_are_hardware_blind():
    """Same discipline over the two new ES modules' own comments and any
    literal strings they carry (chart.js draws from server numbers only and
    has no copy of its own; cloud.js's one literal is the provenance
    fallback empty string). A future edit that hardcodes a fallback label
    here would be exactly the device-taxonomy guess the plan forbids."""
    for path in (CHART_JS, CLOUD_JS):
        _assert_hardware_blind(path.read_text(encoding="utf-8"), source=str(path))
