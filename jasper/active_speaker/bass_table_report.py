# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""One per-level report for the packet index and the command line."""

from __future__ import annotations

from typing import Any, Mapping


def bass_table_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"candidate_id": table["candidate_id"], **row}
            for table in payload.get("tables", ()) for row in table["levels"]]


def bass_table_markdown(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""

    def number(value):
        return f"{value:.1f}" if value is not None else "null"

    def corners(response):
        if not response:
            return "null"
        return " / ".join(number(response["corner_hz"][depth]) + (" (bound)" if response["corner_bounded"][depth] else "")
                          for depth in ("3", "10"))

    lines = ["## Bass by level", "",
             "| Candidate | Main dB | Base / candidate dB SPL | Prescribed dB | Realized dB by Hz band | Base −3 / −10 Hz | Candidate −3 / −10 Hz | Qualified from Hz (base / candidate) | Headroom |",
             "|---|---:|---:|---:|---|---|---|---|---|"]
    for row in rows:
        realized = "; ".join(f"{band['band_hz'][0]:g}–{band['band_hz'][1]:g}: {number(band['value_db'])}"
                             for band in row["realized_boost_db"]) or "null"
        headroom = row["headroom_verdict"] or "unknown"
        for rise in row["headroom_rises"] or ():
            headroom += f"; H{rise['order']} {rise['band_hz'][0]:g}–{rise['band_hz'][1]:g} Hz: {rise['delta_db']:+.1f} dB"
        fields = [row["candidate_id"], number(row["level_key"]["level_db"]),
                  f"{number(row['base_db_spl_at_mark'])} / {number(row['candidate_db_spl_at_mark'])}",
                  number(row["prescribed_boost_db"]), realized,
                  corners(row["base_response"]), corners(row["candidate_response"]),
                  " / ".join(number((row[key] or {}).get("qualified_from_hz")) for key in ("base_response", "candidate_response")),
                  headroom]
        lines.append("| " + " | ".join(str(field).replace("|", "\\|").replace("\n", " ") for field in fields) + " |")
    lines += ["", "Bound marks the qualified floor, not a measured crossing. Prescribed minus realized includes compressor and driver action. "
              "Harmonics use repeat spread, or a 1 dB evidence floor for one repeat; this is not a hearing threshold."]
    return "\n".join(lines)
