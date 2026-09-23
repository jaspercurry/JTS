#!/usr/bin/env bash
# airplay-latency-probe.sh — capture the AirPlay latency budget a real
# sender negotiates with this speaker (and the AP2 stream type), so you
# know whether a bonded leader's downstream delay fits inside it.
#
# WHY: bonded-leader AirPlay lip-sync hinges on the sender's negotiated
# budget vs. the leader's hidden downstream delay. The sender CHOOSES that
# budget live, per session, and shairport already logs it — so this
# probe is READ-ONLY: no config change, no restart. Needs log_verbosity
# >= 1 for the latency line and >= 2 for the stream type. The template
# ships 1, so stream type is unavailable by default — the probe reads the
# effective verbosity up front and says so rather than reporting silence
# as an observation.
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

# Stream-type detection needs log_verbosity >= 2: both markers are debug(2)
# and shairport drops any debug(N) with N > log_verbosity
# (utilities/debug.c `if (level > debuglev) return;`). The latency line is
# debug(1) and still prints. Read-only; an absent or unreadable setting
# leaves this unknown and the probe runs exactly as before.
verbosity="$(ssh "$target" \
  "grep -hE '^[[:space:]]*log_verbosity' /etc/shairport-sync.conf 2>/dev/null" 2>/dev/null \
  | grep -oE '[0-9]+' | tail -1 || true)"
blind=0
if [[ "$verbosity" =~ ^[0-9]+$ ]] && (( verbosity < 2 )); then
    blind=1
    cat <<EOF
!! STREAM TYPE UNAVAILABLE: shairport-sync log_verbosity is ${verbosity} on
!! ${PI_HOST}; realtime-vs-buffered detection needs 2. Both markers are
!! debug(2) — rtsp.c:2890 (UDP realtime) and rtsp.c:2932 (TCP Buffered) —
!! and shairport drops any debug(N) with N > log_verbosity.
!! STILL REPORTED: a NON-DEFAULT negotiated budget, from the debug(1) line
!! at rtp.c:1698. But that line is logged only when the budget != 77175
!! frames, so a default-budget session logs NOTHING and is indistinguishable
!! from no session at all at this verbosity.

EOF
fi

cat <<EOF
Watching shairport-sync on ${PI_HOST} for ${DURATION}s (read-only).
>>> Now: AirPlay audio from a phone/Mac to this speaker. <<<
    A VIDEO app (TV / YouTube / QuickTime) stresses the lip-sync budget best.
    Start/re-start the AirPlay session now. Ctrl-C to stop early.

EOF

tmp="$(mktemp -t airplay-probe.XXXXXX)"
trap 'rm -f "$tmp"' EXIT

# Exact format strings at the pinned SHAIRPORT_SYNC_COMMIT (5.2.3):
#   rtsp.c:2890 debug(2) "... UDP realtime audio port opened: N." -> Realtime
#   rtsp.c:2932 debug(2) "... TCP Buffered Audio port opened: N." -> Buffered
#   rtp.c:1698  debug(1) "Stream-specified latency is N frames. Normally it
#               is 77175."  -> sender budget. AP2-REALTIME path only, and
#               guarded by `!= 77175`, so a default-budget session (every
#               one observed on jts.local so far) prints NOTHING — absence
#               is the default-budget signal the summary below relies on.
# The "AP2 Realtime Audio Stream SETUP." label is debug(4) and the
# "Buffered Audio Stream SETUP" one debug(3) — both above the verbosity
# JTS ships, so neither is usable as a stream-type marker here.
# The port-opened pair IS version-portable: identical debug(2) text in
# 4.3.7, only relocated (rtsp.c:3315 there, rtsp.c:2890 in 5.2.3), so it
# matches on 4.3.7 boxes (jts3, jts4) and on 5.2.3 alike.
# Reading a system unit's journal needs the adm/systemd-journal group
# (the pi user is in adm on Raspberry Pi OS) or sudo; if you hit
# "insufficient permissions", prefix the remote journalctl with `sudo `.
ssh "$target" \
  "timeout ${DURATION} journalctl -u shairport-sync -f -n 0 -o cat 2>/dev/null" \
  | tee "$tmp" \
  | grep --line-buffered -iE 'audio port opened|Stream-specified latency is' || true

echo
echo "================ AirPlay budget summary ================"
# `|| true`: no match makes grep exit 1, and under `set -o pipefail` that
# would abort the script here — before the summary that explains the miss.
stream="$(grep -ioE '(UDP realtime|TCP buffered) audio port opened' "$tmp" \
  | grep -ioE 'realtime|buffered' | tr '[:upper:]' '[:lower:]' | sort -u | paste -sd', ' - || true)"
if [[ -n "$stream" ]]; then
    echo "Stream type(s) seen : ${stream}"
elif (( blind )); then
    echo "Stream type(s) seen : UNAVAILABLE (log_verbosity ${verbosity}, needs 2)"
fi

if grep -qiE 'Stream-specified latency is' "$tmp"; then
    echo "Negotiated latency  : NON-DEFAULT (sender overrode the ~2 s default)"
    grep -ioE 'Stream-specified latency is [0-9]+ frames' "$tmp" | sort -u \
      | sed 's/^/    /'
elif [[ -n "$stream" ]]; then
    echo "Negotiated latency  : DEFAULT (no 'Stream-specified latency' line)"
elif (( blind )); then
    echo "Negotiated latency  : CANNOT OBSERVE at log_verbosity ${verbosity}"
    echo "    Nothing was logged — but at this verbosity a default-budget"
    echo "    session and no session at all are INDISTINGUISHABLE. This run is"
    echo "    not evidence of an idle AirPlay, and not evidence of the default"
    echo "    budget. Raise log_verbosity to 2 and re-run to observe either."
else
    echo "No AirPlay session detected in the window."
    echo "    Start AirPlay audio to ${PI_HOST} and re-run. If you saw a"
    echo "    permissions error above, prefix the remote journalctl with sudo."
fi
if grep -qiE 'Stream-specified latency is' "$tmp" || [[ -n "$stream" ]]; then
    echo "Fit assessment      : see /state or jasper-doctor"
fi
