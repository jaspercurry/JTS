# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only browser for saved speaker measurements and analyses."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from jasper.active_speaker.frequency_view import build_frequency_view
from jasper.active_speaker.measurement_archive import (
    list_measurements,
    load_measurement,
)

from ._common import canonical_header, canonical_page
from .correction_hub import section_tabs

CATALOG_SCHEMA = "jts_frequency_catalog/1"


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


def build_data(
    *,
    sessions_dir: Path,
    run_a_id: str | None = None,
    run_b_id: str | None = None,
) -> dict[str, Any]:
    """Return the archive catalog and neutral A/B frequency view."""

    runs = list_measurements(sessions_dir)
    catalog = [run.to_dict() for run in runs]
    if not runs:
        return {
            "catalog_schema": CATALOG_SCHEMA,
            "catalog": [],
            "selected": {"a": None, "b": None},
            "view": None,
        }

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
