# USB turntable control on JTS3

This is a manual, opt-in proof of concept for controlling a USB turntable from
JTS3. It is deliberately isolated under `experiments/`: nothing is installed by
the normal JTS deploy, no service starts at boot, and no voice or production
runtime path can move the turntable.

JTS owns only the command names and Raspberry Pi power preflight in
[`jts_turntable.py`](jts_turntable.py). The upstream
[`jaspercurry/USB-Turntable`](https://github.com/jaspercurry/USB-Turntable)
repository remains the source of truth for USB discovery, serial framing,
response parsing, and controller behavior through its `usb_turntable` package.
Do not add device protocol bytes or parsing to JTS.

## Pi setup

Use the Pi-side JTS checkout (normally `/home/pi/jts`), not `/opt/jasper`; this
experiment is not part of the installed speaker runtime.

The reusable package is checked in under [`vendor/`](vendor/) so the experiment
works without GitHub credentials or a network install. It has no third-party
runtime dependencies and requires Python 3.10 or newer. Before importing it,
the wrapper verifies a hard-coded SHA-256 of the raw canonical
[`vendor/UPSTREAM.json`](vendor/UPSTREAM.json), then uses that anchored manifest
to verify the exact package inventory, every source and license file hash, and
the aggregate source digest. Unlisted modules, native extensions, bytecode,
directories, or symlinks fail closed before import; verified imports do not
write bytecode into the vendor tree.

The upstream controller is licensed under Apache-2.0. Its exact
[`LICENSE`](vendor/LICENSE) and [`NOTICE.md`](vendor/NOTICE.md) accompany the
vendored source so its redistribution terms and third-party boundary travel
with the snapshot.

```sh
cd /home/pi/jts/experiments/usb-turntable
python3 --version
python3 jts_turntable.py --help
```

The upstream package uses only the standard library. Confirm that the Pi user
can open USB serial devices:

```sh
lsusb
ls -l /dev/serial/by-id/ 2>/dev/null || true
id
```

On Raspberry Pi OS, a serial device commonly belongs to the `dialout` group. If
the selected device does and your user is not a member, add the user once, then
log out and back in (or reboot) before retrying:

```sh
sudo usermod -aG dialout "$USER"
```

## Detect and probe

All options precede the command. `--json` produces one compact JSON object;
without it, the same data is indented for reading.

```sh
cd /home/pi/jts/experiments/usb-turntable

# See whether the upstream discovery rules find the attached controller.
python3 jts_turntable.py detect

# Check the Pi power state before any movement.
python3 jts_turntable.py power

# Open the discovered controller and query product/firmware identity.
python3 jts_turntable.py probe
```

`detect` reports the device `path`, stable `/dev/serial/by-id/...` path when
available, VID/PID, serial number, manufacturer, and product. If more than one
candidate is present, pin the stable path explicitly:

```sh
python3 jts_turntable.py \
  --port /dev/serial/by-id/<controller> probe
```

Detection and probing are read-only and do not require the power gate to pass.

## Manual control

The device's labels are unusual but verified: viewed from above, vendor `Left`
means **clockwise** and vendor `Right` means **counterclockwise**. The wrapper
preserves those labels exactly. Start with a small move and keep the hardware
clear:

```sh
python3 jts_turntable.py left 10
python3 jts_turntable.py right 10

# Save the current position as zero. This does not move the platform.
python3 jts_turntable.py set-zero

# Move back to the saved zero position.
python3 jts_turntable.py home

# Send the vendor stop request without requiring the Pi power preflight.
python3 jts_turntable.py stop
```

`stop` is a software request, not a physical emergency stop. The controller
must first open and synchronize the serial connection, so startup traffic or a
communication failure can delay or prevent transmission. Keep an accessible
hardware power cutoff while testing.

`left`, `right`, and `home` run `vcgencmd get_throttled` first. Current
under-voltage, throttling, frequency-cap, or thermal-limit flags block movement;
an unreadable power status also blocks movement. Since-boot flags are reported
as warnings but do not block a Pi that is healthy now. For a deliberate manual
test only, `--allow-power-risk` overrides the gate for that invocation:

```sh
python3 jts_turntable.py --allow-power-risk left 10
```

Resolve the power problem instead whenever possible.

## What success means

The upstream controller returns structured results rather than treating any
serial line as success:

- `probe` succeeds only when `connected` is true.
- `set-zero` and `stop` complete after the exact acknowledgement and a quiet
  settle; their successful result has `pause_seen=false`.
- `left`, `right`, and `home` complete only after the exact acknowledgement and
  the exact terminal completion event.
- Every operation succeeds only when both `acknowledged` and `completed` are
  true. `pause_seen` separately records an optional pause event during motion.
- `frames` preserves the relevant controller responses for diagnosis.

The upstream package assembles heartbeat hash runs across serial reads and
classifies them after 20 ms of inter-byte quiet. Exact `###` reports remote link
loss, and the controller replies with `#` and waits for a normal heartbeat.
Fresh-open recovery is bounded by `--startup-timeout`; once synchronized,
runtime pre-command and post-settle recovery is bounded by
`--response-timeout`. Four or more consecutive hash bytes fail explicitly. A
recovery error after an acknowledged command means the platform may already
have acted even though this wrapper exits nonzero; inspect the physical
platform before another command.

The wrapper exits `0` only for those strict successes and `1` for no detected
device, a blocked preflight, controller/protocol failure, or an incomplete
operation. Argument errors exit `2`.

## Troubleshooting

**No device in `detect`.** Check `lsusb` and `/dev/serial/by-id/`, reconnect the
USB cable, and confirm the user has permission to open the serial node. Use
`--port` with the stable path if generic USB discovery is ambiguous.

**Permission denied.** Inspect the device owner/group with `ls -l`; add the Pi
user to the owning group (normally `dialout`) and begin a new login session.
Do not run the whole experiment as a persistent root service.

**Startup noise or a probe timeout.** The observed controller can emit one
legacy startup-noise sequence immediately after the serial port opens. The
upstream package accepts only that complete approved sequence plus a normal
heartbeat, then requires a quiet window before sending the first command. Try a
slightly larger bounded startup window and read the synchronization error:

```sh
python3 jts_turntable.py --startup-timeout 5 probe
```

Successful command `frames` begin after synchronization; quarantined startup
bytes are not command responses. Do not work around startup noise by teaching
JTS to accept arbitrary serial lines as acknowledgements; fix that behavior in
the upstream package.

**Heartbeat recovery failure.** An exact `###` heartbeat puts the link into a
bounded recovery state. The controller answers with `#` but sends no new command
until a normal heartbeat arrives. During fresh-open startup, that wait is
bounded by `--startup-timeout`; runtime recovery before a command or after its
settle is bounded by `--response-timeout`. If recovery times out, inspect the
USB path and the platform state before retrying; after-command recovery can fail
after the requested physical action has already occurred.

**Acknowledged but incomplete.** If conditions remain safe, send the vendor
stop request, inspect `frames`, and retry only after confirming the platform is
safe. If the movement is legitimately slow, increase the bounded completion
wait for that run:

```sh
python3 jts_turntable.py --motion-timeout 60 home
```

`--motion-timeout` only bounds how long the program waits for the terminal
completion report. Expiry does not establish whether the platform is still
moving, and it does not cancel or stop platform motion.

**Power preflight blocks movement.** Run `power` and read `current_flags`.
Under-voltage usually means the Pi/USB load needs a stronger supply, shorter
cable, or an appropriately powered USB hub. Since-boot flags remain latched
until reboot, so use `current_flags` to distinguish an active fault from a
recovered earlier event. The explicit override does not make an unstable power
path safe.

**Vendor stop request.** `stop` intentionally bypasses the Pi power preflight,
but it still has to open and synchronize the controller before transmitting.
It can fail and is not a physical emergency stop; keep a hardware power cutoff
accessible.
