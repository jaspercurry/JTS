#!/usr/bin/env bash
# Pull recent logs + relevant configs from the Pi into ./logs/ for
# inspection by Claude Code (or anything else).
#
# Usage:
#   bash scripts/fetch-pi-logs.sh
#   SINCE='10 minutes ago' bash scripts/fetch-pi-logs.sh
#   PI_HOST=192.168.1.42 PI_USER=pi bash scripts/fetch-pi-logs.sh
#
# Each fetch overwrites ./logs/*-latest.* and also keeps a timestamped
# copy under ./logs/. Secrets (API keys) are redacted before write.

set -euo pipefail

PI_HOST="${PI_HOST:-jts.local}"
PI_USER="${PI_USER:-pi}"
SINCE="${SINCE:-1 hour ago}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO_ROOT/logs"
mkdir -p "$OUT"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

echo "Fetching logs from ${PI_USER}@${PI_HOST} (since '$SINCE') → $OUT/" >&2

remote() {
    ssh -o BatchMode=yes -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" "$@"
}

# All units the install script installs, plus the renderers + their
# dependencies. Each is fetched independently and gets its own
# *-latest.log symlink. A unit not installed on this Pi just produces
# an empty log file (journalctl returns 0 with no rows) — that's fine,
# the loop reports "0 lines" and moves on.
units=(
    jasper-camilla
    jasper-voice
    jasper-control
    jasper-mux
    jasper-aec-bridge
    jasper-aec-init
    jasper-dac-init
    jasper-headphone-monitor
    librespot
    shairport-sync
    nqptp
    bluealsa
    bluealsa-aplay
    bt-agent
)

for u in "${units[@]}"; do
    out="$OUT/${u}-${TS}.log"
    if remote "journalctl -u $u --since '$SINCE' --no-pager --output=short-iso 2>/dev/null" > "$out"; then
        size=$(wc -l < "$out")
        echo "  ${u}: ${size} lines" >&2
        ln -sf "$(basename "$out")" "$OUT/${u}-latest.log"
    else
        echo "  ${u}: failed (service may not exist)" >&2
        rm -f "$out"
    fi
done

# Combined log lets you see music + voice + DSP events on one timeline.
# Build the -u flags from the same units list so this stays in sync.
combined_flags=()
for u in "${units[@]}"; do
    combined_flags+=(-u "$u")
done
remote "journalctl --since '$SINCE' --no-pager --output=short-iso \
    ${combined_flags[*]} 2>/dev/null" \
    > "$OUT/combined-${TS}.log"
ln -sf "combined-${TS}.log" "$OUT/combined-latest.log"
echo "  combined: $(wc -l < "$OUT/combined-${TS}.log") lines" >&2

# Configs and runtime state — secrets redacted server-side.
remote "sudo cat /etc/jasper/jasper.env 2>/dev/null \
    | sed -E 's/^(GEMINI_API_KEY|SPOTIFY_CLIENT_SECRET)=.*/\1=<redacted>/'" \
    > "$OUT/jasper.env-${TS}.txt" 2>/dev/null || true
ln -sf "jasper.env-${TS}.txt" "$OUT/jasper.env-latest.txt" 2>/dev/null || true

remote "cat /etc/camilladsp/v1.yml 2>/dev/null" \
    > "$OUT/camilladsp-${TS}.yml" 2>/dev/null || true
ln -sf "camilladsp-${TS}.yml" "$OUT/camilladsp-latest.yml" 2>/dev/null || true

remote "sudo cat /root/.asoundrc 2>/dev/null" \
    > "$OUT/asoundrc-${TS}.txt" 2>/dev/null || true
ln -sf "asoundrc-${TS}.txt" "$OUT/asoundrc-latest.txt" 2>/dev/null || true

remote "echo '== aplay -L =='; aplay -L 2>/dev/null; \
        echo '== arecord -L =='; arecord -L 2>/dev/null; \
        echo '== aplay -l =='; aplay -l 2>/dev/null; \
        echo '== arecord -l =='; arecord -l 2>/dev/null" \
    > "$OUT/alsa-devices-${TS}.txt"
ln -sf "alsa-devices-${TS}.txt" "$OUT/alsa-devices-latest.txt"

remote "systemctl status --no-pager ${units[*]} 2>/dev/null" \
    > "$OUT/systemctl-${TS}.txt" 2>/dev/null || true
ln -sf "systemctl-${TS}.txt" "$OUT/systemctl-latest.txt" 2>/dev/null || true

# Recent voice sessions + spend.
remote "sqlite3 /var/lib/jasper/usage.db 'SELECT id, started_at, ended_at, input_tokens, output_tokens, cost_usd FROM sessions ORDER BY id DESC LIMIT 20' 2>/dev/null" \
    > "$OUT/sessions-${TS}.txt" 2>/dev/null || true
ln -sf "sessions-${TS}.txt" "$OUT/sessions-latest.txt" 2>/dev/null || true

echo "Done. Latest snapshot:" >&2
ls -1 "$OUT"/*-latest.* 2>/dev/null | sed 's|^|  |' >&2
