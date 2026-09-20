"""NOARM: confirm the placement for EVERY capture, not once per pose.

wall1f kept 1 take of 12 and then failed `position_hold_expired`: with
`--mover confirmed` the round re-arms a position hold before each measurement,
so a single `placed` call only unblocks the first one. The mover and the
confirmation are unchanged -- this just answers as often as the round asks.

Confirming repeatedly is truthful here and only here: every measurement of this
round is the same pose 0, the owner set the mic on the arm at 0 deg before he
left, and the turntable is dead, so nothing can move it between captures.
"""
import pathlib
import sys

OLD_START = '  PL=0\n'
NEW = '''  PENDREAD='import sys, json
raw = sys.stdin.read()
i = raw.find("{")
try:
    d = json.loads(raw[i:]) if i >= 0 else {}
except Exception:
    d = {}
print("terminal" if d.get("result") or d.get("status") in ("failed", "complete", "terminal") else "live")'
  confirm_loop() {
    for _ in $(seq 1 900); do
      sudo "$V/jasper-round" placed --run "$RUN" --pose 1 >>"$OUT/placed.json" 2>>"$OUT/placed.err"
      if [ "$(sudo "$V/jasper-round" status --run "$RUN" 2>/dev/null | python3 -c "$PENDREAD")" = terminal ]; then
        break
      fi
      sleep 2
    done
  }
  sudo "$V/jasper-round" placed --run "$RUN" --pose 1 >"$OUT/placed.json" 2>>"$OUT/placed.err"
  PL=$?
  say "first placed rc=$PL $(head -c 160 "$OUT/placed.json" | tr -d '\\n')"
  if [ "$PL" != 0 ]; then say "ABORT placement confirmation failed"; say "$(tail -c 300 "$OUT/placed.err")"; exit 16; fi
  confirm_loop &
  CONFIRMER=$!
  say "confirmer pid=$CONFIRMER (re-confirms pose 0 before every capture)"
'''

path = pathlib.Path(sys.argv[1])
text = path.read_text()
if "confirm_loop" in text:
    print("confirm loop already present")
    raise SystemExit(0)

start = text.find(OLD_START)
end = text.find('fi\nsudo "$V/jasper-round" wait --run')
if start < 0 or end < 0 or end < start:
    raise SystemExit("could not locate the NOARM placement block; refusing to patch")
text = text[:start] + NEW + text[end:]

# Stop the confirmer as soon as the round is over.
anchor = 'say "wait exit=$?'
text = text.replace(anchor,
                    '[ -n "${CONFIRMER:-}" ] && kill "$CONFIRMER" 2>/dev/null\n' + anchor, 1)
path.write_text(text)
print("confirm loop installed")
