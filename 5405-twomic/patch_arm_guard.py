"""Add an arm-at-zero guard to run_round.sh, before the arm gate ever starts.

The speaker now stands ~0.2 m from a wall and the owner gave no swing range, so
a homing move would drive the arm at the wall. Exit 15 = arm not at zero.
"""
import pathlib
import sys

GUARD = '''OFFDEG=$(python3 "$TT" --json offset 2>/dev/null | python3 -c "$OFFREAD")
if [ -z "$OFFDEG" ]; then say "ABORT arm offset unreadable"; exit 15; fi
if ! python3 -c "import sys; sys.exit(0 if abs(float(sys.argv[1])) <= 0.5 else 1)" "$OFFDEG"; then
  say "ABORT arm not at zero ($OFFDEG deg)"; exit 15
fi
say "arm offset $OFFDEG deg - at zero, safe to gate"
'''

READER = """OFFREAD='import sys, json
try:
    d = json.load(sys.stdin)
    v = (d.get("result") or {}).get("offset_degrees")
    print("" if v is None else v)
except Exception:
    print("")'
"""

ANCHOR = 'if ! tt_ok; then say "ABORT turntable does not answer"; exit 12; fi\n'

path = pathlib.Path(sys.argv[1])
text = path.read_text()
if "ABORT arm not at zero" in text:
    print("guard already present")
    raise SystemExit(0)
if ANCHOR not in text:
    raise SystemExit("anchor line not found; refusing to patch")
path.write_text(text.replace(ANCHOR, ANCHOR + READER + GUARD, 1))
print("guard added after the turntable health check")
