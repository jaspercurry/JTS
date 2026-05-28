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
# copy under ./logs/. Secrets are redacted before writing to disk.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
SINCE="${SINCE:-1 hour ago}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO_ROOT/logs"
mkdir -p "$OUT"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

# shellcheck disable=SC1091
. "$REPO_ROOT/scripts/_diagnostic_redaction.sh"

echo "Fetching logs from ${PI_USER}@${PI_HOST} (since '$SINCE') → $OUT/" >&2

remote() {
    ssh -o BatchMode=yes -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" "$@"
}

fetch_remote_bash() {
    local stem="$1"
    local ext="$2"
    local out="$OUT/${stem}-${TS}.${ext}"
    if ssh -o BatchMode=yes -o ConnectTimeout=5 \
            "${PI_USER}@${PI_HOST}" "bash -s" \
            | redact_jasper_diagnostics > "$out"; then
        local size
        size=$(wc -l < "$out")
        echo "  ${stem}: ${size} lines" >&2
        ln -sf "$(basename "$out")" "$OUT/${stem}-latest.${ext}"
    else
        echo "  ${stem}: failed" >&2
        rm -f "$out"
    fi
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
    if remote "journalctl -u $u --since '$SINCE' --no-pager --output=short-iso 2>/dev/null" \
        | redact_jasper_diagnostics > "$out"; then
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
    | redact_jasper_diagnostics \
    > "$OUT/combined-${TS}.log"
ln -sf "combined-${TS}.log" "$OUT/combined-latest.log"
echo "  combined: $(wc -l < "$OUT/combined-${TS}.log") lines" >&2

# Reboot/OOM forensics. These are intentionally lightweight and run
# only when the operator asks for a log bundle; no steady-state daemon
# or metrics store needed. The previous-boot grep is the fast path for
# watchdog/OOM/sudo/SSH smoking guns after a surprise reset.
fetch_remote_bash "reboot-summary" "txt" <<'REMOTE'
set +e
echo "== collection =="
date --iso-8601=seconds
hostname
echo
echo "== boots =="
journalctl --list-boots --no-pager 2>&1
echo
echo "== uptime =="
uptime -s 2>&1
uptime -p 2>&1
echo
echo "== last reboot/shutdown records =="
last -x reboot shutdown 2>/dev/null | head -20
echo
echo "== build =="
sudo cat /var/lib/jasper/build.txt 2>&1
echo
echo "== failed units =="
systemctl --failed --no-pager 2>&1
echo
echo "== memory =="
free -h 2>&1
echo
if command -v swapon >/dev/null 2>&1; then
    swapon --show 2>&1
elif [ -x /usr/sbin/swapon ]; then
    /usr/sbin/swapon --show 2>&1
else
    cat /proc/swaps 2>&1
fi
echo
cat /proc/pressure/memory 2>&1
echo
echo "== cgroup controllers =="
cat /sys/fs/cgroup/cgroup.controllers 2>&1
echo
echo "== jasper unit memory policy/current peaks =="
for unit in \
    jasper-camilla jasper-voice jasper-control jasper-mux \
    jasper-aec-bridge jasper-fanin librespot shairport-sync \
    bluealsa bluealsa-aplay ssh; do
    echo "-- ${unit} --"
    systemctl show "$unit" \
        -p Slice -p OOMScoreAdjust -p MemoryCurrent -p MemoryPeak \
        -p MemoryHigh -p MemoryMax -p MemorySwapMax --no-pager 2>&1
done
echo
echo "== slice memory events =="
for dir in \
    /sys/fs/cgroup/jts.slice \
    /sys/fs/cgroup/jts.slice/jts-audio.slice \
    /sys/fs/cgroup/jts.slice/jts-mic.slice; do
    [ -d "$dir" ] || continue
    echo "-- $dir --"
    cat "$dir/memory.current" 2>/dev/null
    cat "$dir/memory.peak" 2>/dev/null
    cat "$dir/memory.events" 2>/dev/null
    cat "$dir/memory.swap.current" 2>/dev/null
done
echo
echo "== pi throttling =="
if command -v vcgencmd >/dev/null 2>&1; then
    vcgencmd get_throttled 2>&1
else
    echo "vcgencmd unavailable"
fi
true
REMOTE

fetch_remote_bash "previous-boot-forensics" "log" <<'REMOTE'
set +e
echo "== previous boot OOM/watchdog/reset clues =="
journalctl -b -1 --no-pager --output=short-iso 2>/dev/null \
    | grep -Ei 'out of memory|invoked oom-killer|killed process|oom-kill|watchdog|hung task|blocked for more than|panic|kernel bug|segfault|reboot|shutdown|power' \
    | tail -1000
echo
echo "== previous boot operator breadcrumbs =="
journalctl -b -1 --no-pager --output=short-iso 2>/dev/null \
    | grep -Ei 'sshd.*Accepted|sshd.*session opened|sshd.*session closed|sshd.*Received disconnect|sshd.*Disconnected|sudo\[[0-9]+\]:.*COMMAND=' \
    | grep -Evi 'sshd-session\[[0-9]+\]: Connection closed by 127\.0\.0\.1' \
    | grep -Ei 'sshd|COMMAND=(/opt/jasper/\.venv/bin/python -|/usr/bin/python3? -|python3? -|(/usr/bin/)?systemd-run .*--unit=jts-diagnostic-|bash /home/pi/jts/deploy/install\.sh|/usr/bin/cat /var/lib/jasper/build\.txt)' \
    | sed -E 's#(COMMAND=(/usr/bin/)?systemd-run .*-- /usr/bin/bash -lc ).*#\1<diagnostic-command-redacted>#' \
    | tail -200
true
REMOTE

fetch_remote_bash "previous-boot-kernel" "log" <<'REMOTE'
set +e
echo "== previous boot kernel warnings/errors =="
journalctl -b -1 -k -p warning..alert --no-pager --output=short-iso 2>/dev/null \
    | tail -1000
echo
echo "== previous boot kernel tail =="
journalctl -b -1 -k --no-pager --output=short-iso 2>/dev/null | tail -300
true
REMOTE

fetch_remote_bash "current-boot-kernel" "log" <<'REMOTE'
set +e
journalctl -b 0 -k -p warning..alert --no-pager --output=short-iso 2>/dev/null
true
REMOTE

# Configs and runtime state — secrets redacted before write.
remote "sudo sh -c 'for f in \
        /etc/jasper/jasper.env \
        /var/lib/jasper/voice_provider.env \
        /var/lib/jasper/google_credentials.env \
        /var/lib/jasper/home_assistant.env \
        /var/lib/jasper/transit.env \
        /var/lib/jasper/wifi_guardian.env; do \
            [ -r \"\$f\" ] || continue; \
            echo \"== \$f ==\"; \
            cat \"\$f\"; \
        done' 2>/dev/null" \
    | redact_jasper_diagnostics \
    > "$OUT/jasper.env-${TS}.txt" 2>/dev/null || true
ln -sf "jasper.env-${TS}.txt" "$OUT/jasper.env-latest.txt" 2>/dev/null || true

remote "cat /etc/camilladsp/v1.yml 2>/dev/null" \
    > "$OUT/camilladsp-${TS}.yml" 2>/dev/null || true
ln -sf "camilladsp-${TS}.yml" "$OUT/camilladsp-latest.yml" 2>/dev/null || true

# /etc/asound.conf since 2026-05-23 (PR #223) — moved from
# /root/.asoundrc so non-root renderer users (shairport-sync, pi)
# can resolve user-space PCM names. Fall back to /root/.asoundrc
# if we're talking to a pre-PR-#223 Pi.
remote "cat /etc/asound.conf 2>/dev/null || sudo cat /root/.asoundrc 2>/dev/null" \
    > "$OUT/asoundrc-${TS}.txt" 2>/dev/null || true
ln -sf "asoundrc-${TS}.txt" "$OUT/asoundrc-latest.txt" 2>/dev/null || true

remote "echo '== aplay -L =='; aplay -L 2>/dev/null; \
        echo '== arecord -L =='; arecord -L 2>/dev/null; \
        echo '== aplay -l =='; aplay -l 2>/dev/null; \
        echo '== arecord -l =='; arecord -l 2>/dev/null" \
    > "$OUT/alsa-devices-${TS}.txt"
ln -sf "alsa-devices-${TS}.txt" "$OUT/alsa-devices-latest.txt"

remote "systemctl status --no-pager ${units[*]} 2>/dev/null" \
    | redact_jasper_diagnostics \
    > "$OUT/systemctl-${TS}.txt" 2>/dev/null || true
ln -sf "systemctl-${TS}.txt" "$OUT/systemctl-latest.txt" 2>/dev/null || true

# Recent voice sessions + spend.
remote "sqlite3 /var/lib/jasper/usage.db 'SELECT id, started_at, ended_at, input_tokens, output_tokens, cost_usd FROM sessions ORDER BY id DESC LIMIT 20' 2>/dev/null" \
    > "$OUT/sessions-${TS}.txt" 2>/dev/null || true
ln -sf "sessions-${TS}.txt" "$OUT/sessions-latest.txt" 2>/dev/null || true

echo "Done. Latest snapshot:" >&2
ls -1 "$OUT"/*-latest.* 2>/dev/null | sed 's|^|  |' >&2
