# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""One per-level report for the packet index and the command line."""

from __future__ import annotations

from typing import Any, Mapping

BASS_READOUT_FIELDS = (
    "candidate_id", "level_key", "base_db_spl_at_mark", "candidate_db_spl_at_mark",
    "prescribed_boost_db", "realized_boost_db", "compression_db", "compression_includes", "base_response", "candidate_response",
    "headroom_verdict", "headroom_rises", "headroom", "snr_margin_db", "repeat_spread_db", "position_spread_db",
)


def bass_table_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{key: table["candidate_id"] if key == "candidate_id" else row.get(key) for key in BASS_READOUT_FIELDS}
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

    def ladder(row, key):
        fields = []
        for stack, bands in (row.get("headroom") or {}).items():
            for band in bands:
                value = number(band[key])
                if key == "knee_level_db_spl" and band["knee_bounded"]:
                    value += f" (above top measurable rung: {number(band['top_clean_level_db_spl'])} dB SPL)"
                if key == "knee_level_db_spl" and band.get("unmeasured_level_keys"):
                    value += "; unmeasured Main dB: " + ", ".join(number(level["level_db"]) for level in band["unmeasured_level_keys"])
                if key == "headroom_remaining_db" and band[key] is not None:
                    value += " (measured; extrapolated)" if band["extrapolated"] else " (measured)"
                fields.append(f"{stack} {band['band_hz'][0]:g}–{band['band_hz'][1]:g}: {value}")
        return "; ".join(fields) or "null"

    lines = ["## Bass by level", "",
             "| Candidate | Main dB | Base / candidate dB SPL | Law dB | Prescribed / realized dB by Hz band | Base −3 / −10 Hz | Candidate −3 / −10 Hz | Qualified from Hz (base / candidate) | Headroom | Knee dB SPL by Hz band | Headroom remaining dB by Hz band |",
             "|---|---:|---:|---:|---|---|---|---|---|---|---|"]
    for row in rows:
        realized = "; ".join(f"{band['band_hz'][0]:g}–{band['band_hz'][1]:g}: {number(band.get('prescribed_boost_db'))} / {number(band['value_db'])}"
                             for band in row["realized_boost_db"]) or "null"
        headroom = row["headroom_verdict"] or "unknown"
        for rise in row["headroom_rises"] or ():
            headroom += f"; H{rise['order']} {rise['band_hz'][0]:g}–{rise['band_hz'][1]:g} Hz: {rise['delta_db']:+.1f} dB"
        floors = [(row[key] or {}).get("qualified_from_hz") for key in ("base_response", "candidate_response")]
        qualified = "No qualified evidence" if all(floor is None for floor in floors) and all(
            band["value_db"] is None for band in row["realized_boost_db"]) else " / ".join(map(number, floors))
        fields = [row["candidate_id"], number(row["level_key"]["level_db"]),
                  f"{number(row['base_db_spl_at_mark'])} / {number(row['candidate_db_spl_at_mark'])}",
                  number(row["prescribed_boost_db"]), realized,
                  corners(row["base_response"]), corners(row["candidate_response"]),
                  qualified, headroom, ladder(row, "knee_level_db_spl"), ladder(row, "headroom_remaining_db")]
        lines.append("| " + " | ".join(str(field).replace("|", "\\|").replace("\n", " ") for field in fields) + " |")
    includes = ", ".join(sorted({cause.replace("_", " ") for row in rows
                                 for cause in row.get("compression_includes") or ()})) or "unknown"
    lines += ["", "Bound marks the qualified floor, not a measured crossing. Prescribed boost models CamillaDSP's shelf, any Linkwitz shape and the delta high-pass at the recorded fader. "
              f"Band means use the same qualified bins as realized boost; their difference includes: {includes}. "
              "Within-level harmonic deltas use repeat spread, or a 1 dB evidence floor for one repeat; this is not a hearing threshold. "
              "Across-level knees use repeat spread or level uncertainty from the worst SNR; headroom uses measured SPL and marks an unreached knee as extrapolated."]
    return "\n".join(lines)
