# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only active-speaker measurement browser."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from ._common import canonical_header, canonical_page
from .correction_hub import section_tabs

CATALOG_SCHEMA = "jts_frequency_catalog/1"


class MeasurementViewRequestError(ValueError):
    """A requested retained measurement run is unavailable."""


def render_page(hostname: str, csrf_token: str = "") -> bytes:
    escaped_host = html.escape(hostname, quote=True)
    body = f"""
{canonical_header("Measurements", back_href=f"http://{escaped_host}/sound/crossover/")}
<main class="page crossover-measurements-page">
  {section_tabs("crossover")}

  <section class="info-card info-card--accent">
    <p class="eyebrow">Saved measurements</p>
    <h2 class="section__title">Frequency response</h2>
    <p class="info-card__note">Choose one run, or add a second run for an A/B view.</p>
  </section>

  <section class="info-card measurement-run-pickers" aria-label="Measurement runs">
    <label>Run A<select id="measurement-run-a"></select></label>
    <label>Run B<select id="measurement-run-b"><option value="">None</option></select></label>
  </section>

  <section class="info-card">
    <div class="measurement-chart-wrap">
      <canvas id="measurement-chart" aria-label="Saved frequency response measurements"></canvas>
    </div>
    <p id="measurement-chart-status" class="info-card__note" role="status" aria-live="polite">Loading measurements…</p>
    <div id="measurement-series" class="measurement-series"></div>
  </section>

  <section id="measurement-metadata" class="measurement-metadata" aria-label="Run details"></section>
</main>
<script type="module" src="/assets/correction/js/crossover/measurements.js"></script>
"""
    return canonical_page(
        "Crossover measurements — JTS speaker",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/correction/crossover-measurements.css",
    )


def _catalog(sessions_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    from jasper.active_speaker import bundles
    from jasper.active_speaker.crossover_v2.evidence_packet import round_artifact_dir

    public: list[dict[str, Any]] = []
    paths: dict[str, Path] = {}
    for entry in bundles.list_bundles(sessions_dir):
        run_id = str(entry.get("session_id") or "")
        bundle_dir = Path(str(entry.get("bundle_dir") or ""))
        round_dir, _ = round_artifact_dir(bundle_dir)
        if not run_id or round_dir is None:
            continue
        paths[run_id] = bundle_dir
        public.append({
            "id": run_id,
            "started_at": entry.get("started_at"),
            "state": entry.get("state"),
        })
    return public, paths


def build_data(
    *,
    sessions_dir: Path,
    run_a_id: str | None = None,
    run_b_id: str | None = None,
) -> dict[str, Any]:
    """Return the run catalog and the shared frequency view for A/B."""

    from jasper.active_speaker.crossover_v2.evidence_packet import (
        CrossoverEvidencePacketError,
        build_crossover_evidence_packet,
    )
    from jasper.active_speaker.crossover_v2.frequency_view import build_frequency_view

    catalog, paths = _catalog(sessions_dir)
    if not catalog:
        return {
            "catalog_schema": CATALOG_SCHEMA,
            "catalog": [],
            "selected": {"a": None, "b": None},
            "view": None,
        }

    selected_a = run_a_id or catalog[0]["id"]
    selected_b = run_b_id or None
    missing = [run_id for run_id in (selected_a, selected_b) if run_id and run_id not in paths]
    if missing:
        raise MeasurementViewRequestError(f"measurement run not found: {missing[0]}")

    try:
        packet_a = build_crossover_evidence_packet(paths[selected_a])
        packet_b = (
            build_crossover_evidence_packet(paths[selected_b])
            if selected_b is not None else None
        )
    except CrossoverEvidencePacketError as exc:
        raise MeasurementViewRequestError(str(exc)) from exc

    return {
        "catalog_schema": CATALOG_SCHEMA,
        "catalog": catalog,
        "selected": {"a": selected_a, "b": selected_b},
        "view": build_frequency_view(packet_a, packet_b),
    }
