# USB turntable control on JTS3

This is a manual, opt-in experiment for controlling the USB turntable from
JTS3. It has no installer hook, systemd unit, voice tool, or production daemon.
Run it only from a JTS checkout when you intend to move the platform.

JTS owns the operator-facing labels and Raspberry Pi power preflight in
[`jts_turntable.py`](jts_turntable.py). The bundled upstream `usb_turntable`
package owns USB discovery, serial transport, protocol parsing, heartbeat
recovery, operation completion, and timeout defaults. Do not duplicate those
details in JTS.

## Setup

Use the Pi-side checkout, normally `/home/pi/jts`, rather than `/opt/jasper`:

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
```

`detect` reports upstream discovery records. If more than one candidate is
present, select the stable path explicitly:

```sh
python3 jts_turntable.py --port /dev/serial/by-id/<controller> probe
```

Detection and probing do not run the Pi power gate.

## Manual control

The device labels are unusual but verified: viewed from above, vendor `Left`
means clockwise and vendor `Right` means counterclockwise.

```sh
python3 jts_turntable.py left 10
python3 jts_turntable.py right 10
python3 jts_turntable.py set-zero   # records the current position; no movement
python3 jts_turntable.py home
python3 jts_turntable.py stop       # sends the vendor stop request
```

`left`, `right`, and `home` first run the bounded `vcgencmd get_throttled`
preflight. Active under-voltage, frequency capping, throttling, thermal limiting,
or an unreadable power status blocks movement. Since-boot flags remain visible
but do not block a Pi that is healthy now.

For a deliberate manual test, the explicit override applies to one invocation:

```sh
python3 jts_turntable.py --allow-power-risk left 10
```

Resolve the power problem instead whenever possible.

## Guarded measurement positions

The microphone rig's saved zero is the acoustic on-axis home. For automated
measurements, use signed absolute positions: negative is left and positive is
right. Each invocation runs the Pi power preflight, opens the controller once,
returns fully to home, and only then makes one relative move to the requested
angle. It refuses targets outside the inclusive `-45` to `+45` degree envelope.

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
home before using this flag after every power cycle.

The observed hard-left limit is approximately `-52` degrees from home; never
target it. The exact hard-right limit is unknown and unnecessary. Automated
measurement motion stays within `-45` to `+45` degrees. A useful smoke-test
sweep is `0, -10, -20, 0, +10, +20, 0`. Start a measurement only after the
position command reports `ok: true`, and always finish by commanding position
`0`.

## Results and safety boundary

Output is structured JSON; `--json` selects the compact form. `probe` succeeds
only when the upstream result says it is connected. Other operations succeed
only when the upstream result is both acknowledged and completed. The wrapper
exits `0` for success, `1` for a device, power, controller, or incomplete-result
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
  platform. Retry only when its state is known and the area is clear.
- **Need to request stop:** run `stop`, while remembering that software delivery
  is not guaranteed; use the hardware cutoff if conditions are unsafe.
