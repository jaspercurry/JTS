#!/usr/bin/env bash
# airplay-latency-probe.sh — capture the AirPlay latency budget a real
# sender negotiates with this speaker (and the AP2 stream type), so you
# know whether a bonded leader's downstream delay fits inside it.
#
# WHY: bonded-leader AirPlay lip-sync (docs/HANDOFF-airplay.md, "AirPlay 2
# latency is sender-authored — the bonded-leader consequence") hinges on
# the sender's negotiated budget vs. the leader's hidden downstream delay
# (~150 ms pipeline + the Snapcast buffer_ms). The sender CHOOSES that
# budget live, per session, and shairport already logs it
# (log_verbosity = 2 in deploy/shairport-sync.conf.template) — so this
# probe is READ-ONLY: no config change, no restart.
#
# USAGE:
#   bash scripts/airplay-latency-probe.sh             # watch for 120 s
#   DURATION=300 bash scripts/airplay-latency-probe.sh
#   PI_HOST=jts3.local bash scripts/airplay-latency-probe.sh
#
# While it runs, AirPlay audio from a phone/Mac to this speaker. A VIDEO
# app (TV, YouTube/Safari, QuickTime) stresses the lip-sync budget best.
# Start (or re-start) the AirPlay session AFTER launching the probe — the
# latency/stream-type lines are logged at session SETUP.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=_lib.sh
. ./_lib.sh

DURATION="${DURATION:-120}"
# Validate before interpolating into the remote command — DURATION is
# operator-supplied and flows into `ssh host "timeout $DURATION ..."`.
if ! [[ "$DURATION" =~ ^[0-9]+$ ]]; then
    echo "DURATION must be a whole number of seconds (got: '${DURATION}')" >&2
    exit 2
fi
target="${PI_USER}@${PI_HOST}"

cat <<EOF
Watching shairport-sync on ${PI_HOST} for ${DURATION}s (read-only).
>>> Now: AirPlay audio from a phone/Mac to this speaker. <<<
    A VIDEO app (TV / YouTube / QuickTime) stresses the lip-sync budget best.
    Start/re-start the AirPlay session now. Ctrl-C to stop early.

EOF

tmp="$(mktemp -t airplay-probe.XXXXXX)"
trap 'rm -f "$tmp"' EXIT

# shairport logs at verbosity 2 on JTS, so both lines below are present:
#   "... AP2 Realtime/Buffered Audio Stream."  -> stream type (rtsp.c)
#   "Notified latency is N frames."            -> sender budget, ONLY if N != 77175
# Reading a system unit's journal needs the adm/systemd-journal group
# (the pi user is in adm on Raspberry Pi OS) or sudo; if you hit
# "insufficient permissions", prefix the remote journalctl with `sudo `.
ssh "$target" \
  "timeout ${DURATION} journalctl -u shairport-sync -f -n 0 -o cat 2>/dev/null" \
  | tee "$tmp" \
  | grep --line-buffered -iE 'Audio Stream\.|Notified latency is' || true

echo
echo "================ AirPlay budget summary ================"
stream="$(grep -ioE '(Realtime|Buffered) Audio Stream' "$tmp" | sort -u | paste -sd', ' -)"
[[ -n "$stream" ]] && echo "Stream type(s) seen : ${stream}"

if grep -qiE 'Notified latency is' "$tmp"; then
    echo "Negotiated latency  : NON-DEFAULT (sender overrode the ~2 s default)"
    grep -ioE 'Notified latency is [0-9]+ frames' "$tmp" | sort -u | while read -r line; do
        frames="$(printf '%s' "$line" | grep -oE '[0-9]+')"
        # AirPlay frames are 44100 Hz; total scheduled latency adds shairport's
        # fixed +11035 (the value the backend offset lives inside).
        secs="$(awk -v f="$frames" 'BEGIN{printf "%.3f", (f+11035)/44100}')"
        echo "    ${line}  -> ~${secs}s total scheduled latency"
    done
    echo "TIGHT-REGIME CHECK  : compare that budget against 150 ms + your"
    echo "    Snapcast buffer_ms (default 400 ms => need ~0.55 s). If the"
    echo "    budget is below that, expect bounded residual lip-sync lag when bonded."
elif [[ -n "$stream" ]]; then
    echo "Negotiated latency  : DEFAULT (no 'Notified latency' line)"
    echo "    => 77175 frames (~1.75 s) + 11035 = ~2.0 s budget. FREE regime:"
    echo "    a bonded leader's ~150 ms + Snapcast buffer_ms fits with >1 s"
    echo "    to spare; a bond-aware offset fully hides it."
else
    echo "No AirPlay session detected in the window."
    echo "    Start AirPlay audio to ${PI_HOST} and re-run. If you saw a"
    echo "    permissions error above, prefix the remote journalctl with sudo."
fi
