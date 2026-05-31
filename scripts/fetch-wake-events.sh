#!/usr/bin/env bash
# Pull the wake-event corpus (SQLite DB + per-event WAVs) from the Pi
# into ./wake-events/ for manual review + labeling.
#
# Usage:
#   bash scripts/fetch-wake-events.sh
#   PI_HOST=192.168.1.42 bash scripts/fetch-wake-events.sh
#   NO_OPEN=1 bash scripts/fetch-wake-events.sh    # skip the Finder pop-up
#
# Pulls into ./wake-events/<UTC-timestamp>/:
#   wake-events.sqlite3                  Full DB snapshot (per-event funnel + scores + labels)
#   <event_id>.aec-on.wav                Audio per leg, 6 s window (4 s pre + 2 s post fire)
#   <event_id>.aec-off.wav
#   index.csv                            CSV of full per-event metadata, sorted newest-first
#                                        (opens cleanly in Numbers / Excel / Sheets)
#   index.tsv                            Same info as TSV (grep-friendly)
#
# Also overwrites ./wake-events/latest → most recent fetch dir, for
# convenience. On macOS, pops open the folder in Finder at the end so
# you can listen to clips immediately (set NO_OPEN=1 to skip).
#
# The SQLite file is read-only on the Pi (active jasper-voice
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
# Glob expansion must run as root because /var/lib/jasper/ is mode
# 0750 root:root — the unprivileged shell that builds the cp command
# silently fails to expand ${REMOTE_SRC}/*.wav, leaving us with an
# empty staging dir. Wrap in 'sudo bash -c' so the shell that does
# the expansion is the root shell.
sudo bash -c 'cp -a ${REMOTE_SRC}/*.wav /tmp/wake-events-fetch/ 2>/dev/null || true'
sudo chown -R ${PI_USER}:${PI_USER} /tmp/wake-events-fetch
"

# Step 3: rsync the staging area back. -a preserves mtime so the
# laptop sees the same chronological order as the Pi.
rsync -avz "${PI_USER}@${PI_HOST}:/tmp/wake-events.fetch.sqlite3" \
    "$OUT/wake-events.sqlite3" >/dev/null
rsync -avz "${PI_USER}@${PI_HOST}:/tmp/wake-events-fetch/" "$OUT/" \
    --exclude wake-events.fetch.sqlite3 >/dev/null

# Step 4: drop CSV + TSV indexes for at-a-glance browsing. CSV is
# the primary format (opens cleanly in Numbers / Excel / Sheets),
# TSV stays for grep-friendly power users. Both sorted newest-first
# so the most recent events are at the top of the file. Uses Python
# stdlib so no extra deps.
python3 - <<PY
import csv, sqlite3, sys
db = "${OUT}/wake-events.sqlite3"
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
# All columns the row review benefits from. Order chosen for
# spreadsheet readability: identifiers first, then scoring data,
# then context, then label fields at the end (where the user types).
cols = [
    "ts_utc", "event_id", "trigger_kind",
    "peak_score_aec_on", "peak_score_aec_off", "peak_score_dtln_aec",
    "peak_score_chip_aec_150", "peak_score_chip_aec_210",
    "peak_offset_ms_on", "peak_offset_ms_off", "peak_offset_ms_dtln",
    "peak_offset_ms_chip_aec_150", "peak_offset_ms_chip_aec_210",
    "outcome", "outcome_detail",
    "mic_muted",
    "mic_rms_dbfs_on", "mic_rms_dbfs_off", "mic_rms_dbfs_dtln",
    "mic_rms_dbfs_chip_aec_150", "mic_rms_dbfs_chip_aec_210",
    "music_active", "music_volume_db",
    "voice_provider", "wake_model", "threshold",
    "audio_on_path", "audio_off_path", "audio_dtln_path",
    "audio_chip_aec_150_path", "audio_chip_aec_210_path",
    "label", "label_notes",
]
existing = {r[1] for r in conn.execute("PRAGMA table_info(wake_events)")}
missing = [c for c in cols if c not in existing]
if missing:
    print(
        "  warning: wake_events DB lacks index columns: "
        + ", ".join(missing),
        file=sys.stderr,
    )
cols = [c for c in cols if c in existing]
rows = list(conn.execute(
    f"SELECT {', '.join(cols)} FROM wake_events ORDER BY ts_utc DESC"
))
# CSV — primary review format
with open("${OUT}/index.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(cols)
    for r in rows:
        w.writerow([r[c] if r[c] is not None else "" for c in cols])
# TSV — kept for backward compat + grep/awk
with open("${OUT}/index.tsv", "w") as f:
    f.write("\t".join(cols) + "\n")
    for r in rows:
        vals = []
        for c in cols:
            v = r[c] if r[c] is not None else ""
            # Tabs/newlines in free-form fields (outcome_detail,
            # label_notes) would corrupt TSV — flatten to spaces.
            if isinstance(v, str):
                v = v.replace("\t", " ").replace("\n", " ")
            vals.append(str(v))
        f.write("\t".join(vals) + "\n")
conn.close()
print(f"  index.csv + index.tsv: {len(rows)} events (newest first)")
PY

# Step 5: update the "latest" pointer for convenience
ln -snf "${TS}" "${REPO_ROOT}/wake-events/latest"

# Step 6: clean up the Pi-side staging
ssh "${PI_USER}@${PI_HOST}" "
sudo rm -f /tmp/wake-events.fetch.sqlite3
sudo rm -rf /tmp/wake-events-fetch
"

echo "" >&2
echo "Done. ${OUT}/ contains:" >&2
echo "  - $(ls "$OUT/" | grep -c '\.wav$') WAVs (one file per captured wake leg; legacy events have fewer legs)" >&2
echo "  - index.csv (spreadsheet) + index.tsv (grep)" >&2
echo "  - wake-events.sqlite3 (full DB snapshot)" >&2
echo "" >&2
echo "Symlink: wake-events/latest -> ${TS}" >&2
echo "" >&2
echo "To label an event after listening:" >&2
echo "  sqlite3 '$OUT/wake-events.sqlite3' \\" >&2
echo "    \"UPDATE wake_events SET label='real_attempt', label_notes='clear hey jarvis' WHERE event_id='...'\"" >&2
echo "" >&2
echo "To sanity-check the corpus integrity (xcorr alignment + DB completeness):" >&2
echo "  bash scripts/audit-wake-events.sh" >&2

# Step 7: pop the folder open in Finder so you can listen to the
# clips immediately. macOS-only; skipped on Linux / when NO_OPEN
# is set. Doesn't error out if `open` isn't available.
if [[ -z "${NO_OPEN:-}" ]] && command -v open >/dev/null 2>&1; then
    open "$OUT" 2>/dev/null && echo "Finder: opened ${OUT}" >&2 || true
fi
