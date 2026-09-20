"""Add NOARM=1 to run_round.sh: front mic only, no turntable, no arm gate.

USB bus 1 died with the xHCI controller, taking the CH341 turntable adapter and
the Dayton with it. NOARM=1 drops every part of the round that needs either:
the turntable health check and re-bind, the arm-zero guard, the side recording
and the angle-capture gate. The mover becomes `confirmed` and the runner
confirms the one pending placement itself -- truthful, because the owner set the
mic on the arm at 0 deg before leaving and nothing can move it now.
Exit 16 = the placement confirmation never went through.
"""
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text()
if "NOARM" in text:
    print("NOARM mode already present")
    raise SystemExit(0)

lines = text.splitlines(keepends=True)
out = []
guard_open = False
for line in lines:
    stripped = line.strip()

    if stripped.startswith("tt_ok() {") and not guard_open:
        out.append('NOARM="${NOARM:-0}"\n')
        out.append('[ "$NOARM" = 1 ] && say "NOARM: no turntable, no arm gate, no side mic; '
                   'mover=confirmed"\n')
        out.append('if [ "$NOARM" != 1 ]; then\n')
        guard_open = True

    if guard_open and stripped.startswith("say \"arm offset $OFFDEG deg"):
        out.append(line)
        out.append("fi\n")
        guard_open = False
        continue

    if stripped.startswith('[ "${NOSIDE:-0}" = 1 ] || setsid nohup nice'):
        line = line.replace('[ "${NOSIDE:-0}" = 1 ]',
                            '[ "${NOSIDE:-0}" = 1 ] || [ "$NOARM" = 1 ]', 1)

    elif stripped.startswith('setsid nohup "$V/jasper-angle-capture" serve'):
        out.append('if [ "$NOARM" != 1 ]; then\n')
        out.append(line)
        out.append("fi\n")
        continue

    elif "--mover arm" in line:
        line = line.replace('--mover arm', '--mover "$MOVER"', 1)
        out.append('MOVER=arm; [ "$NOARM" = 1 ] && MOVER=confirmed\n')

    elif stripped.startswith("sleep 20; while ps -eo args"):
        line = '[ "$NOARM" = 1 ] || { sleep 20; while ps -eo args | grep -qE ' \
               '"[a]ngle-capture serve"; do sleep 5; done; }\n'

    elif stripped.startswith('sudo "$V/jasper-round" wait --run'):
        out.append('if [ "$NOARM" = 1 ]; then\n')
        out.append('  PL=0\n')
        out.append('  for i in $(seq 1 24); do\n')
        out.append('    sudo "$V/jasper-round" placed --run "$RUN" --pose 1 '
                   '>"$OUT/placed.json" 2>>"$OUT/placed.err" && { PL=1; break; }\n')
        out.append('    sleep 5\n')
        out.append('  done\n')
        out.append('  say "placed try=$i ok=$PL $(head -c 200 "$OUT/placed.json" | tr -d \'\\n\')"\n')
        out.append('  if [ "$PL" != 1 ]; then say "ABORT placement confirmation failed"; '
                   'say "$(tail -c 300 "$OUT/placed.err")"; exit 16; fi\n')
        out.append("fi\n")
        line = line.replace("--timeout 600", '--timeout "${WAITT:-600}"', 1)

    elif stripped.startswith('say "retakes:'):
        line = line.replace('arm offset $(python3 "$TT" --json offset 2>/dev/null | cut -c1-90)',
                            'arm offset $([ "$NOARM" = 1 ] && echo "not read (NOARM)" || '
                            'python3 "$TT" --json offset 2>/dev/null | cut -c1-90)', 1)

    out.append(line)

if guard_open:
    raise SystemExit("guard block was never closed; refusing to write")
path.write_text("".join(out))
print("NOARM mode added")
