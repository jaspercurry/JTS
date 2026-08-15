# USB turntable control on JTS3

This is an experimental controller for the USB turntable on JTS3. Positioning
remains manual and opt-in: there is no voice tool, measurement scheduler, or
permanent polling daemon. The sole automatic behavior is a bounded one-shot
stop request when the turntable's USB serial adapter appears.

JTS owns the operator-facing labels and Raspberry Pi power preflight in
[`jts_turntable.py`](jts_turntable.py). The bundled upstream `usb_turntable`
package owns USB discovery, serial transport, protocol parsing, heartbeat
recovery, operation completion, and timeout defaults. Do not duplicate those
details in JTS.

The vendor parser occasionally raises `ProtocolError` when a periodic
heartbeat byte lands inside a response frame mid-exchange -- a transport-layer
parse race, not a real command failure. Its effect outlives the moment it's
raised: the vendor's own parser buffer is left non-empty, so the *very next*
command on that same serial session fails closed too, for an unrelated reason
("pre-command receive was not quiescent"). An in-process retry that reused the
same session therefore could not recover. JTS layers one bounded retry of the
whole operation for `offset`, `probe`, and the guarded `position` (see "Guarded
measurement positions" below) against a **freshly opened controller session**
-- a brand-new serial open plus the vendor's own synchronization, exactly what
a second CLI invocation does. Only the vendored transport's exact
`ProtocolError` base class is retried; a real command outcome reported as a
more specific subclass (an unconfirmed motion, a rejected command, a genuine
link timeout) is never retried and propagates on the first attempt, so an
unconfirmed motion is never silently re-commanded. `detect` never touches the
serial link, so it can't raise this error at all. This never applies to
`set-zero`, `left`, `right`, `stop`, or `home`, which stay zero-retry.

## Setup

Use the Pi-side checkout, normally `/home/pi/jts`, for manual commands. The
installer separately stages the same experiment under `/opt/jasper` for the
hot-plug one-shot:

```sh
cd /home/pi/jts/experiments/usb-turntable
python3 --version                 # Python 3.10 or newer
python3 jts_turntable.py --help
lsusb
ls -l /dev/serial/by-id/ 2>/dev/null || true
```

The upstream package is bundled under [`vendor/`](vendor/) for offline use and
has no third-party runtime dependencies. [`vendor/UPSTREAM.json`](vendor/UPSTREAM.json)
records the reviewed repository commit and source aggregate. That is
development-time provenance checked by tests, not runtime authentication of a
hostile local checkout. Apache-2.0 [`LICENSE`](vendor/LICENSE) and
[`NOTICE.md`](vendor/NOTICE.md) travel with the source.

The serial device commonly belongs to the `dialout` group. If needed, add the
Pi user once, then start a new login session:

```sh
sudo usermod -aG dialout "$USER"
```

## Detect and probe

```sh
python3 jts_turntable.py detect
python3 jts_turntable.py power
python3 jts_turntable.py probe
python3 jts_turntable.py offset
```

`detect` reports upstream discovery records. If more than one candidate is
present, select the stable path explicitly:

```sh
python3 jts_turntable.py --port /dev/serial/by-id/<controller> probe
```

`offset` queries the controller's signed offset from its saved zero and never
sends a motion command. It only reports whether the arm is at the
controller's believed zero — never whether that belief is still the acoustic
axis. The vendor controller's own `return_to_zero` uses this same
`offset == 0` check purely to skip a redundant move, not as a correctness
check, and it is not one here either: `offset` can never substitute for the
physical check `--confirm-zero-valid` records. Use it only as a same-session
drift screen — a nonzero reading with no intervening power event means
something moved and is worth investigating before continuing. Right after a
power event it is actively misleading: if the controller's position counter
does not survive power loss, it can silently adopt whatever position the arm
is currently sitting in as its new zero, so `offset` reads `0.00` even though
the arm may be off the real acoustic axis — a full sweep would then measure
the wrong axis with no error at all. Only a human looking at the rig can
confirm zero is still correct after a power event.

Detection, probing, and the offset query do not run the Pi power gate.

## Manual control

The device labels are unusual but verified: viewed from above, vendor `Left`
means clockwise and vendor `Right` means counterclockwise.

```sh
python3 jts_turntable.py left 10
python3 jts_turntable.py right 10
python3 jts_turntable.py set-zero --confirm-redefine-zero
python3 jts_turntable.py home
python3 jts_turntable.py stop       # sends the vendor stop request
```

**`set-zero` destroys the saved acoustic-axis zero.** It overwrites the
turntable's saved zero with the current physical position; there is no undo.
Every guarded `position` call trusts that saved zero, so redefining it without
the rig actually sitting on-axis silently invalidates every measurement taken
afterward. `--confirm-redefine-zero` is required — omitting it refuses before
opening the controller (exit `2`, mirroring an invalid-argument failure, not a
normal `ok: false` result). `set-zero` is FORBIDDEN in automated measurement
flows; an automated caller that suspects zero has drifted should stop and page
an operator, never redefine zero itself. The co-installed vendored CLI
(`python3 -m usb_turntable set-zero`, alias `zero`) performs the same
destructive write with no confirmation gate at all — install stages the whole
vendored package onto the Pi alongside this wrapper, and automated flows must
never invoke it as a workaround. Use `offset` (above) as a drift screen before
a session, but see "Detect and probe" above for why it cannot confirm zero is
still valid on its own.

`left`, `right`, and `home` first run the bounded `vcgencmd get_throttled`
preflight. Active under-voltage, frequency capping, throttling, thermal limiting,
or an unreadable power status blocks movement. Since-boot flags remain visible
but do not block a Pi that is healthy now.

For a deliberate manual test, the explicit override applies to one invocation:

```sh
python3 jts_turntable.py --allow-power-risk left 10
```

Resolve the power problem instead whenever possible.

## Hot-plug stop

A full JTS install stages
`99-jasper-turntable-autostop.rules` and the hardened
`jasper-turntable-autostop@.service` template. When a CH340 tty with USB ID
`1a86:7523` appears, udev passes that event's exact `/dev/ttyUSB…` path to one
systemd job. The rule also requires JTS3's confirmed dedicated physical USB
path, `platform-xhci-hcd.1-usb-0:2:1.0`; moving the cable to another USB port
disables automatic stop. The helper opens only that path, verifies product
`MT320RUBL40ProV3` in the same controller session, and sends the vendor stop
request. A different CH340 product necessarily receives the non-motion
connection, firmware, and product identity queries, then is logged and ignored
without receiving STOP or any motion command.

The helper exits immediately after an acknowledged and completed stop. While
the tty or controller is still settling, it makes at most four attempts with
1.5 seconds between failed attempts; systemd also caps the entire job at 40
seconds. There is no timer or resident process. If the USB device disappears,
the device-bound job is stopped; reconnecting it creates a new one-shot job.

Inspect its structured journal events with:

```sh
journalctl -u 'jasper-turntable-autostop@*.service' --no-pager
```

Success has `"event": "turntable_autostop.stopped"` in its JSON record. Retry,
exhaustion, invalid-device, and non-turntable CH340 outcomes use the stable
`turntable_autostop.retry`, `.exhausted`, `.rejected`, and `.ignored` event
names. USB IDs alone are not identity because `1a86:7523` is generic; the
same-session product probe adds a short delay but prevents an unrelated CH340
device from receiving the stop command.

## Guarded measurement positions

The microphone rig's saved zero is the acoustic on-axis home. For automated
measurements, use signed absolute positions: negative is left and positive is
right. Each invocation runs the Pi power preflight, opens the controller,
returns fully to home, and only then makes one relative move to the requested
angle. It refuses targets outside the inclusive `-45` to `+45` degree envelope.

The vendored transport's exact `ProtocolError` base class anywhere in that
home-then-move sequence is retried once as a whole, against a **freshly
opened controller session** (a brand-new serial open, not the raced one
re-used, and not a second run of the Pi power preflight, which already
passed before the first attempt): the retry re-homes from scratch before
moving again, so a race that lands mid-home or mid-move cannot leave the arm
double-moved. A more specific
`ProtocolError` subclass -- a motion acknowledged but never confirmed
complete, a rejected command, a genuine link timeout -- reports a real
command outcome, not a parse race, and is never retried. A response with
`"retried": true` means the vendor transport raced once and the fresh session
recovered; a second failure still returns `ok: false` and reports both
attempts' errors.

Both confirmations are required on every invocation so an unattended caller
cannot silently assume the physical setup is safe:

```sh
python3 jts_turntable.py position -20 \
  --confirm-rig-clear --confirm-zero-valid
python3 jts_turntable.py position 0 \
  --confirm-rig-clear --confirm-zero-valid
```

`--confirm-rig-clear` means the arm's full path is physically clear.
`--confirm-zero-valid` means an operator has confirmed that the controller's
saved zero is still the acoustic on-axis position since its latest power-on.
Zero persistence across a controller power cycle is unverified, so reconfirm
home before using this flag after every power cycle — a `0.00` reading from
`offset` is not sufficient evidence on its own; see "Detect and probe" above
for why.

The observed hard-left limit is approximately `-52` degrees from home; never
target it. The exact hard-right limit is unknown and unnecessary. Automated
measurement motion stays within `-45` to `+45` degrees. A useful smoke-test
sweep is `0, -10, -20, 0, +10, +20, 0`. Start a measurement only after the
position command reports `ok: true`, and always finish by commanding position
`0`.

## Results and safety boundary

Output is structured JSON; `--json` selects the compact form. `probe`
succeeds only when the upstream result says it is connected; `offset`
succeeds only when the upstream result is acknowledged (it has no completion
phase — it never moves anything). Every other operation succeeds only when
the upstream result is both acknowledged and completed. The wrapper exits `0`
for success, `1` for a device, power, controller, or incomplete-result
failure, and `2` for invalid arguments.

The bundled controller owns bounded startup synchronization, heartbeat recovery,
and response/completion waits. A software timeout does not establish whether
the platform is moving and does not cancel or stop platform motion. A failure
reported after acknowledgement may occur after the platform has already acted;
inspect it before another command.

`stop` is a vendor software request, not a physical emergency stop. Opening and
synchronizing the serial controller can delay or prevent transmission. Keep an
accessible hardware power cutoff while testing.

## Troubleshooting

- **No device:** reconnect USB, inspect `lsusb` and `/dev/serial/by-id/`, and use
  `--port` if discovery is ambiguous.
- **Permission denied:** inspect the device owner/group and begin a new session
  after adding the user to `dialout`. Do not make this a persistent root service.
- **Power blocked:** run `power` and read `current_flags`. Improve the supply,
  cable, or powered USB hub rather than relying on the override.
- **Controller or timeout error:** inspect the structured error and physical
  platform. `offset`, `probe`, and the guarded `position` already retried
  once automatically against a fresh controller session when the failure was
  the vendor transport's own parse race (`"retried": true` on success
  confirms one was absorbed); a reported failure either already survived
  that or was a real command outcome (acknowledged-but-incomplete, rejected,
  timed out) that never auto-retries in the first place. `detect` and
  `set-zero`/`left`/`right`/`stop`/`home` never auto-retry at all. In every
  case, only retry by hand once the platform's state is known and the area
  is clear.
- **Need to request stop:** run `stop`, while remembering that software delivery
  is not guaranteed; use the hardware cutoff if conditions are unsafe.
