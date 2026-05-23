#!/usr/bin/env bash
# Reset the wake-event corpus on the Pi, starting a clean week of
# data collection. Designed for the triple-stream architecture
# rollout: after the feature deploys, run this to wipe the
# pre-triple-stream legacy events + any test fires, so the
# upcoming week's data is a clean slate for analysis.
#
# What this does (atomic from the daemon's perspective):
#   1. Stops jasper-voice (releases the SQLite connection +
#      capture-ring file handles)
#   2. Archives the current corpus to
#      /var/lib/jasper/wake-events-archive-<UTC-timestamp>/
#      (preserves pre-reset data offline so nothing's lost)
#   3. Recreates an empty /var/lib/jasper/wake-events/ with the
#      correct permissions (mode 0755, root:root)
#   4. Restarts jasper-voice — schema migration runs on open(),
#      recreates the SQLite DB
#   5. Logs `event=wake_events.reset` with the archive path to the
#      journal so the reset has an audit trail
#
# Usage:
#   bash scripts/reset-wake-events.sh
#   PI_HOST=192.168.1.42 bash scripts/reset-wake-events.sh
#   DRY_RUN=1 bash scripts/reset-wake-events.sh    # print what would happen
#
# To inspect an archive later:
#   ssh pi@jts.local 'ls /var/lib/jasper/wake-events-archive-*'
#   bash scripts/fetch-wake-events.sh  # only fetches the LIVE corpus —
#                                       # archives stay on the Pi
#
# To restore an archive (rare; e.g. you accidentally reset and want
# the data back):
#   ssh pi@jts.local '
#     sudo systemctl stop jasper-voice
#     sudo rm -rf /var/lib/jasper/wake-events
#     sudo mv /var/lib/jasper/wake-events-archive-<TS> /var/lib/jasper/wake-events
#     sudo systemctl start jasper-voice
#   '

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
DRY_RUN="${DRY_RUN:-}"

ARCHIVE_TS="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE_DIR="/var/lib/jasper/wake-events-archive-${ARCHIVE_TS}"
LIVE_DIR="/var/lib/jasper/wake-events"

if [[ -n "$DRY_RUN" ]]; then
    cat <<EOF
DRY_RUN — would execute on ${PI_USER}@${PI_HOST}:
  sudo systemctl stop jasper-voice
  sudo mv ${LIVE_DIR} ${ARCHIVE_DIR}
  sudo install -d -m 0755 -o root -g root ${LIVE_DIR}
  sudo systemctl start jasper-voice
  sudo logger -t jasper "event=wake_events.reset archive=${ARCHIVE_DIR}"
EOF
    exit 0
fi

echo "Resetting wake-event corpus on ${PI_USER}@${PI_HOST}" >&2
echo "  Archive target: ${ARCHIVE_DIR}" >&2

ssh "${PI_USER}@${PI_HOST}" "set -euo pipefail
# Sanity-check before we touch anything destructive. Needs sudo
# because /var/lib/jasper is 0750 root:root — pi user can't even
# stat the wake-events subdir without it.
if ! sudo test -d '${LIVE_DIR}'; then
    echo 'wake-events dir does not exist; nothing to reset' >&2
    exit 1
fi

# Pre-reset stats so the user sees what they're archiving
ev_count=\$(sudo /opt/jasper/.venv/bin/python -c \"
import sqlite3
try:
    c = sqlite3.connect('${LIVE_DIR}/wake-events.sqlite3')
    print(c.execute('SELECT COUNT(*) FROM wake_events').fetchone()[0])
except Exception:
    print('?')
\")
wav_count=\$(sudo bash -c 'ls ${LIVE_DIR}/*.wav 2>/dev/null | wc -l' | tr -d ' ')
dir_size=\$(sudo du -sh '${LIVE_DIR}' | cut -f1)
echo \"  pre-reset: \${ev_count} events, \${wav_count} WAVs, \${dir_size} on disk\"

# Stop the daemon — releases the SQLite file handle and the
# capture-ring buffers. Without this, the mv would race the
# active writer.
sudo systemctl stop jasper-voice

# Archive (mv is atomic on the same filesystem so the corpus is
# never partially present in two places).
sudo mv '${LIVE_DIR}' '${ARCHIVE_DIR}'

# Recreate empty live dir with the correct ownership + mode.
# install.sh uses the same line — matched here so a fresh dir
# behaves identically to a freshly-installed dir.
sudo install -d -m 0755 -o root -g root '${LIVE_DIR}'

# Restart the daemon. jasper-voice's WakeEventStore.open() runs
# the schema migration, populating the empty dir with a fresh
# wake-events.sqlite3 + WAL files.
sudo systemctl start jasper-voice

# Audit trail to the journal — searchable later for 'when did we
# reset' questions.
sudo logger -t jasper \"event=wake_events.reset archive=${ARCHIVE_DIR}\"

# Brief confirmation
sleep 1
echo
echo \"Reset complete.\"
echo \"  Archive: ${ARCHIVE_DIR}\"
echo \"  jasper-voice: \$(systemctl is-active jasper-voice)\"
"
