#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Refresh jasper/data/mta_stations.csv from MTA's "Subway Stations" dataset
# on data.ny.gov (dataset id 39hk-dx4f). The dataset is updated by MTA
# whenever stations open/close/rename; run this script ~yearly or after
# any visible service change to keep our station table accurate.
#
# Schema produced (CSV with `#` comment header preserved):
#   stop_id,stop_name,borough,lines,lat,lon,north_label,south_label
#
# Two consumers:
#   - jasper/subway.py uses {stop_id, stop_name, borough, lines,
#     north_label, south_label} for voice-direction labelling
#   - jasper/transit/providers/nyc_subway.py uses {stop_id, stop_name,
#     lat, lon, lines} for nearest-stop discovery from a user's coords
#
# DictReader ignores unknown columns, so extending the schema with
# lat/lon is backward-compatible.
#
# Idempotent: same inputs → same output. Safe to commit the diff.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT="${REPO_ROOT}/jasper/data/mta_stations.csv"

# Keep the staging file ON the same filesystem as OUT so the Python
# os.replace at the bottom is a true atomic rename. /tmp is often a
# separate filesystem (especially on macOS dev machines) and os.replace
# across filesystems raises OSError: EXDEV.
RAW="${OUT}.fetch.tmp"
trap 'rm -f "${RAW}" "${OUT}.tmp"' EXIT

# data.ny.gov Socrata CSV endpoint. $limit=600 covers the 496-station
# dataset with headroom; $select trims to the columns we use so the
# response stays small (~30 KB).
URL="https://data.ny.gov/resource/39hk-dx4f.csv?\$limit=600&\$select=gtfs_stop_id,stop_name,borough,daytime_routes,gtfs_latitude,gtfs_longitude,north_direction_label,south_direction_label"

echo "Fetching MTA Subway Stations from data.ny.gov..."
# Retry 3x with 2 s delay so a transient Socrata 503 doesn't fail the
# refresh outright. --fail still exits non-zero on a final 4xx/5xx.
curl --silent --show-error --fail --max-time 30 \
    --retry 3 --retry-delay 2 \
    "${URL}" -o "${RAW}"
fetched=$(grep -c '' "${RAW}" || echo 0)
echo "  ${fetched} lines fetched"

# Row-count sanity guard: refuse to write a near-empty CSV. Saw 0-row
# responses from Socrata once during a backend bug — silently shipping
# an empty bundled CSV would disable every speaker's subway lookup.
if [[ "${fetched}" -lt 400 ]]; then
    echo "Refusing to refresh: fetched only ${fetched} lines (expected ~497)." >&2
    echo "Inspect ${RAW} and re-run." >&2
    exit 1
fi

python3 - "${RAW}" "${OUT}" <<'PY'
"""Transform Socrata CSV → our schema. Writes atomically (tempfile + rename).

Direction labels (north_label / south_label) are HAND-CURATED in many cases —
MTA's official labels for some stations are bland ("Southbound", "Last Stop")
where a destination-anchored label ("Coney Island", "Far Rockaway") makes
voice answers materially better. The refresh policy:

  - lat/lon/lines/stop_name/borough: always taken from MTA (authoritative).
  - north_label/south_label: KEEP existing hand-curation when present, only
    fill in from MTA for stations not yet in the file.

To override a label, hand-edit the CSV. The next refresh preserves it.
"""
import csv, os, sys

raw_path, out_path = sys.argv[1], sys.argv[2]

HEADER_COMMENT = """\
# MTA NYC subway stations. Pulled from data.ny.gov dataset 39hk-dx4f
# ("MTA Subway Stations") via scripts/refresh-mta-stations.sh; rerun
# that script to refresh after service changes.
#
# Schema:
#   stop_id     — GTFS parent stop id (e.g. "B12", "R01"). Platforms are
#                 <id>N (north / often uptown / Manhattan-bound) and <id>S
#                 (south / often downtown / Coney-bound).
#   stop_name   — display name as MTA prints it.
#   borough     — Bk / Bx / M / Q / SI.
#   lines       — semicolon-joined daytime routes serving this station.
#   lat, lon    — WGS84 from GTFS, used for nearest-stop discovery.
#   north_label,
#   south_label — Direction labels. Hand-curated overrides preserved by
#                 the refresh script; missing rows pull MTA defaults.
#                 Edit by hand to make voice aliasing snappier.
#
# Two consumers:
#   - jasper/subway.py  → voice-direction labelling for arrivals tool
#   - jasper/transit/providers/nyc_subway.py
#                       → nearest-stop discovery from user's coords
"""

# Existing hand-curated labels, if the file already exists. We only
# refresh when MTA's data has a NEW station not yet in our file.
existing_labels: dict[str, tuple[str, str]] = {}
if os.path.exists(out_path):
    with open(out_path, newline="", encoding="utf-8") as f:
        non_comment = (line for line in f if not line.lstrip().startswith("#"))
        for row in csv.DictReader(non_comment):
            sid = (row.get("stop_id") or "").strip()
            if sid:
                existing_labels[sid] = (
                    (row.get("north_label") or "").strip(),
                    (row.get("south_label") or "").strip(),
                )

with open(raw_path, newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

# Sort by stop_id for stable diffs across refreshes.
rows.sort(key=lambda r: r["gtfs_stop_id"])

preserved = 0
tmp = out_path + ".tmp"
with open(tmp, "w", newline="", encoding="utf-8") as f:
    f.write(HEADER_COMMENT)
    w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
    w.writerow(["stop_id", "stop_name", "borough", "lines",
                "lat", "lon", "north_label", "south_label"])
    for r in rows:
        sid = r["gtfs_stop_id"]
        # daytime_routes is space-separated in the source; emit
        # semicolon-joined so a value with spaces survives CSV parsing
        # unambiguously (subway.py splits on both ; and whitespace).
        lines = ";".join(r["daytime_routes"].split())
        if sid in existing_labels:
            n_label, s_label = existing_labels[sid]
            preserved += 1
        else:
            n_label = r["north_direction_label"]
            s_label = r["south_direction_label"]
        w.writerow([
            sid, r["stop_name"], r["borough"], lines,
            r["gtfs_latitude"], r["gtfs_longitude"],
            n_label, s_label,
        ])

os.replace(tmp, out_path)
print(f"Wrote {out_path} ({len(rows)} stations, "
      f"{preserved} hand-curated labels preserved).")
PY
