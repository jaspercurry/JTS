# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only browser for saved speaker measurements and analyses."""

from __future__ import annotations

import html
from dataclasses import replace
from pathlib import Path
from typing import Any

from jasper.active_speaker.frequency_view import build_frequency_view
from jasper.active_speaker.measurement_archive import (
    ArchivedMeasurement,
    list_measurements,
    load_measurement,
)

from ._common import canonical_header, canonical_page
from .correction_hub import section_tabs

CATALOG_SCHEMA = "jts_frequency_catalog/1"

#: Namespaces a banked round's catalog id against a live session id of the
#: same name. The id is opaque to the page, which hands it back verbatim.
BANKED_ID_PREFIX = "round:"


class MeasurementViewRequestError(ValueError):
    """A requested retained measurement is unavailable."""


def render_page(hostname: str, csrf_token: str = "") -> bytes:
    escaped_host = html.escape(hostname, quote=True)
    body = f"""
{canonical_header("Measurements", back_href=f"http://{escaped_host}/")}
<main class="page measurements-page">
  {section_tabs("measurements")}

  <section class="info-card info-card--accent">
    <p class="eyebrow">Saved measurements</p>
    <h2 class="section__title">Frequency response</h2>
    <p class="info-card__note">Choose one measurement, or add a second for an A/B view.</p>
  </section>

  <section class="info-card measurement-run-pickers" aria-label="Measurements">
    <label>Measurement A<select id="measurement-run-a"></select></label>
    <label>Measurement B<select id="measurement-run-b"><option value="">None</option></select></label>
  </section>

  <section class="info-card">
    <div class="measurement-chart-wrap">
      <canvas id="measurement-chart" aria-label="Saved frequency response measurements"></canvas>
    </div>
    <p id="measurement-chart-status" class="info-card__note" role="status" aria-live="polite">Loading measurements…</p>
    <div id="measurement-series" class="measurement-series"></div>
  </section>

  <section id="measurement-metadata" class="measurement-metadata" aria-label="Measurement details"></section>
</main>
<script type="module" src="/assets/correction/js/measurements.js"></script>
"""
    return canonical_page(
        "Measurements — JTS speaker",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/correction/measurements.css",
    )


def _banked_rounds(campaign_root: Path) -> tuple[ArchivedMeasurement, ...]:
    """The campaign home's rounds, read as archive entries.

    A banked round keeps its session bundle under the ``bundle/`` root that
    ``round_inputs`` owns, so the archive's own reader lists it unchanged and
    applies the same "carries measurements" filter; only the id is the round's.
    """

    from jasper.active_speaker.crossover_v2.round_inputs import (
        RoundViewsError,
        round_inputs,
    )

    root = Path(campaign_root)
    if not root.is_dir():
        return ()
    entries: list[ArchivedMeasurement] = []
    for round_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        try:
            inputs = round_inputs(round_dir)
        except RoundViewsError:
            continue
        # Only the banked shape: the live one resolves its own directory, whose
        # parent is the campaign root — every round at once, under one id.
        if not inputs.banked:
            continue
        entries.extend(
            replace(run, id=f"{BANKED_ID_PREFIX}{round_dir.name}")
            for run in list_measurements(inputs.session_dir.parent)
        )
    return tuple(entries)


def _newest_first(run: ArchivedMeasurement) -> float:
    """Sort key over both halves of the catalog; an unreadable time sorts last."""

    try:
        return -float(run.started_at)
    except (TypeError, ValueError):
        return 0.0


def _catalog_entry(run: ArchivedMeasurement) -> dict[str, Any]:
    """One picker row: the selector, plus what it is and what to call it."""

    return {
        **run.to_dict(),
        "name": run.id.removeprefix(BANKED_ID_PREFIX),
        "origin": "banked" if run.id.startswith(BANKED_ID_PREFIX) else "live",
    }


def build_data(
    *,
    sessions_dir: Path,
    campaign_root: Path,
    run_a_id: str | None = None,
    run_b_id: str | None = None,
) -> dict[str, Any]:
    """Return the archive catalog and neutral A/B frequency view.

    The catalog is the live bundles under ``sessions_dir`` and the rounds
    banked under ``campaign_root``, newest first; both are selected the same
    way, by their catalog id.
    """

    runs = sorted(
        (*list_measurements(sessions_dir), *_banked_rounds(campaign_root)),
        key=_newest_first,
    )
    if not runs:
        return {
            "catalog_schema": CATALOG_SCHEMA,
            "catalog": [],
            "selected": {"a": None, "b": None},
            "view": None,
        }
    catalog = [_catalog_entry(run) for run in runs]

    by_id = {run.id: run for run in runs}
    selected_a = run_a_id or runs[0].id
    selected_b = run_b_id or None
    for run_id in (selected_a, selected_b):
        if run_id and run_id not in by_id:
            raise MeasurementViewRequestError(f"measurement not found: {run_id}")
    run_a = load_measurement(by_id[selected_a])
    run_b = load_measurement(by_id[selected_b]) if selected_b is not None else None

    return {
        "catalog_schema": CATALOG_SCHEMA,
        "catalog": catalog,
        "selected": {"a": selected_a, "b": selected_b},
        "view": build_frequency_view(run_a, run_b),
    }
