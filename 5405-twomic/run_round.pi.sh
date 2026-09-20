#!/usr/bin/env bash
# INFORMAL two-mic round runner for jts3 (#5405, owner-requested, not product code).
# One product trial with the arm; the Dayton iMM-6C is recorded on the side into RAM.
# usage: run_round.sh <label> <candidates-csv | -> [poses]      (run as pi)
set -u
LABEL="$1"; CANDS="$2"; POSES="${3:-rear/express}"
BASE_FP="a81d6c56dff1da3635d8e6f9ce11c507d43ab89a28db447319e497a16e2ac4b4"
OUT="/home/pi/ab-5405/twomic/rounds/$LABEL"; RAM="/dev/shm/twomic-$LABEL"
TT="/opt/jasper/experiments/usb-turntable/jts_turntable.py"; V="/opt/jasper/.venv/bin"
mkdir -p "$OUT" "$RAM"; exec >>"$OUT/runner.log" 2>&1
say() { echo "$(date -Is) $*"; }
say "== round $LABEL cands=$CANDS poses=$POSES"
if ps -eo args | grep -E "[a]ngle-capture serve|[j]asper-round (run|trial|wait|apply)|[j]asper-seat-level|[i]nstall.sh|[a]record " | grep -q .; then say "ABORT box busy"; exit 10; fi
if curl -s http://127.0.0.1:8780/volume | grep -q '"muted": true'; then say "ABORT output muted"; exit 11; fi
tt_ok() { python3 "$TT" --json offset 2>/dev/null | grep -q '"ok": true'; }
if ! tt_ok; then say "turntable silent: re-binding usb port"; P=$(readlink -f /sys/class/tty/ttyUSB0/device | sed -E 's|.*/usb[0-9]+/([^/]+)/.*|\1|'); echo "$P" | sudo tee /sys/bus/usb/drivers/usb/unbind >/dev/null; sleep 3; echo "$P" | sudo tee /sys/bus/usb/drivers/usb/bind >/dev/null; sleep 15; fi
if ! tt_ok; then say "ABORT turntable does not answer"; exit 12; fi
date +%s.%N > "$OUT/side-start-epoch.txt"
[ "${NOSIDE:-0}" = 1 ] || setsid nohup nice -n 19 ionice -c3 arecord -q -D hw:iMM6C -f S24_3LE -r 48000 -c 1 --buffer-size=480000 -d 3000 "$RAM/side-dayton.wav" >"$OUT/arecord.log" 2>&1 </dev/null &
sleep 2
setsid nohup "$V/jasper-angle-capture" serve --attest-rig-clear --poll-s 6 --hostname jts3.local --base-url http://127.0.0.1 --trail "$OUT/arm-trail.jsonl" >"$OUT/arm-gate.log" 2>&1 </dev/null &
sleep 3
T0=$(date "+%Y-%m-%d %H:%M:%S")
CARG=(); [ "$CANDS" = "-" ] || CARG=(--candidates "$CANDS")   # "-" = pair (branches) round on the applied tune
RUN=$(sudo "$V/jasper-round" run --program rear --poses "$POSES" --repeats "${REPEATS:-1}" --mover arm "${CARG[@]}" 2>>"$OUT/run.err" | tee "$OUT/run.json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('run_id') or '')")
if [ -z "$RUN" ]; then say "ABORT run refused"; cat "$OUT/run.json" | head -c 600; pkill -INT -x arecord; pkill -f "angle-capture serve"; exit 13; fi
say "run_id=$RUN"
sleep 20; while ps -eo args | grep -qE "[a]ngle-capture serve"; do sleep 5; done
sudo "$V/jasper-round" wait --run "$RUN" --timeout 600 >"$OUT/wait.json" 2>"$OUT/wait.err" </dev/null
say "wait exit=$? $(grep -E '"result"|"round_dir"' "$OUT/wait.json" | tr -d '\n' | cut -c1-200)"
pkill -INT -x arecord; sleep 2
[ -f "$RAM/side-dayton.wav" ] && mv "$RAM/side-dayton.wav" "$OUT/side-dayton.wav"; rmdir "$RAM" 2>/dev/null
sudo journalctl --since "$T0" --no-pager -o short-iso-precise | grep -E "program_playback action=(start|end)|session_graph action=install|event=program_analysis\.(anchor|glitch|frame_ledger|capture_integrity)" | cut -c1-420 > "$OUT/journal-takes.txt"
for i in $(seq 1 40); do ps -eo args | grep -qE "[a]ngle-capture serve" || break; sleep 3; done
NOW=$(sudo "$V/jasper-crossover-prescriber" status | python3 -c "import sys,json; print(json.load(sys.stdin)['applied']['candidate_fingerprint'])")
say "applied after round: $NOW"; [ "$NOW" = "$BASE_FP" ] || { say "ABORT applied tune changed"; exit 14; }
say "retakes: $(grep -c capture_overrun "$OUT/journal-takes.txt") overrun lines; side wav $(stat -c %s "$OUT/side-dayton.wav" 2>/dev/null || echo none) bytes; arm offset $(python3 "$TT" --json offset 2>/dev/null | cut -c1-90)"
say "== done $LABEL"
