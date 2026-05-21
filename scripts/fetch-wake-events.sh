#!/usr/bin/env bash
# Pull the wake-event corpus (SQLite DB + per-event WAVs) from the Pi
# into ./wake-events/ for manual review + labeling.
#
# Usage:
#   bash scripts/fetch-wake-events.sh
#   PI_HOST=192.168.1.42 bash scripts/fetch-wake-events.sh
#
# Pulls into ./wake-events/<UTC-timestamp>/:
#   wake-events.sqlite3                  Full DB snapshot (per-event funnel + scores + labels)
#   <event_id>.aec-on.wav                Audio per leg, 6 s window (4 s pre + 2 s post fire)
#   <event_id>.aec-off.wav
#   index.tsv                            Tab-separated summary for quick browsing in any editor
#
# Also overwrites ./wake-events/latest → most recent fetch dir, for
# convenience. The SQLite file is read-only on the Pi (active jasper-voice
# is writing it); we use sqlite3's ".backup" to get a consistent snapshot
# without grabbing a write lock.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$REPO_ROOT/wake-events/${TS}"
mkdir -p "$OUT"

echo "Fetching wake-events corpus from ${PI_USER}@${PI_HOST} → $OUT/" >&2

REMOTE_SRC=/var/lib/jasper/wake-events

# Step 1: consistent DB snapshot via Python's sqlite3.backup (no
# external sqlite3 CLI required on the Pi). Writes to a tmpfile owned
# by the user we sshed in as so rsync can pull it without sudo.
ssh "${PI_USER}@${PI_HOST}" "sudo /opt/jasper/.venv/bin/python -c \"
import sqlite3
src = sqlite3.connect('${REMOTE_SRC}/wake-events.sqlite3')
dst = sqlite3.connect('/tmp/wake-events.fetch.sqlite3')
src.backup(dst)
src.close()
dst.close()
\" && sudo chown ${PI_USER}:${PI_USER} /tmp/wake-events.fetch.sqlite3"

# Step 2: prepare the WAVs in a sudo-readable staging area (Pi-side).
# The wake-events dir is mode 0755 but the parent /var/lib/jasper is
# 0750 root:root, so we can't rsync directly without sudo. Symlinks
# into /tmp work for both files.
ssh "${PI_USER}@${PI_HOST}" "
sudo rm -rf /tmp/wake-events-fetch
sudo mkdir -p /tmp/wake-events-fetch
sudo cp -a ${REMOTE_SRC}/*.wav /tmp/wake-events-fetch/ 2>/dev/null || true
sudo chown -R ${PI_USER}:${PI_USER} /tmp/wake-events-fetch
"

# Step 3: rsync the staging area back. -a preserves mtime so the
# laptop sees the same chronological order as the Pi.
rsync -avz "${PI_USER}@${PI_HOST}:/tmp/wake-events.fetch.sqlite3" \
    "$OUT/wake-events.sqlite3" >/dev/null
rsync -avz "${PI_USER}@${PI_HOST}:/tmp/wake-events-fetch/" "$OUT/" \
    --exclude wake-events.fetch.sqlite3 >/dev/null

# Step 4: drop a TSV index for at-a-glance browsing. Uses Python
# stdlib so no extra deps. Includes the wall-clock, peak scores,
# funnel outcome, label, and the audio paths.
python3 - <<PY
import sqlite3, os
db = "${OUT}/wake-events.sqlite3"
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
rows = list(conn.execute("""
  SELECT event_id, ts_utc, trigger_kind,
         peak_score_aec_on, peak_score_aec_off,
         outcome, outcome_detail,
         audio_on_path, audio_off_path,
         label, label_notes
  FROM wake_events
  ORDER BY ts_utc DESC
"""))
with open("${OUT}/index.tsv", "w") as f:
    f.write("ts_utc\tevent_id\ttrigger\tscore_on\tscore_off\toutcome\tlabel\taudio_on\taudio_off\tdetail\n")
    for r in rows:
        f.write("\t".join([
            r["ts_utc"] or "",
            r["event_id"],
            r["trigger_kind"] or "",
            f"{r['peak_score_aec_on']:.3f}" if r["peak_score_aec_on"] is not None else "",
            f"{r['peak_score_aec_off']:.3f}" if r["peak_score_aec_off"] is not None else "",
            r["outcome"] or "",
            r["label"] or "",
            r["audio_on_path"] or "",
            r["audio_off_path"] or "",
            (r["outcome_detail"] or "").replace("\t", " ").replace("\n", " "),
        ]) + "\n")
conn.close()
print(f"  index.tsv: {len(rows)} events")
PY

# Step 5: update the "latest" pointer for convenience
ln -snf "${TS}" "${REPO_ROOT}/wake-events/latest"

# Step 6: clean up the Pi-side staging
ssh "${PI_USER}@${PI_HOST}" "
sudo rm -f /tmp/wake-events.fetch.sqlite3
sudo rm -rf /tmp/wake-events-fetch
"

echo "" >&2
echo "Done. Files in $OUT/:" >&2
ls -lh "$OUT/" | awk 'NR>1 {print "  " $NF " (" $5 ")"}' >&2
echo "" >&2
echo "Symlink: $REPO_ROOT/wake-events/latest -> ${TS}" >&2
echo "" >&2
echo "Browse: open '$OUT/index.tsv' in any editor (TSV; tabs separate columns)" >&2
echo "Query:  sqlite3 '$OUT/wake-events.sqlite3' (or use any SQLite GUI)" >&2
echo "" >&2
echo "To label an event later:" >&2
echo "  sqlite3 '$OUT/wake-events.sqlite3' \\" >&2
echo "    \"UPDATE wake_events SET label='real_attempt', label_notes='clear hey jarvis' WHERE event_id='...'\"" >&2
