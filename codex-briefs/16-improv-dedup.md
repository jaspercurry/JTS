# Brief 16 — Shared Improv module: end the dial/satellite onboarding clone

Mission: review §1.8 + the satellite_onboard dive (5/10) —
jasper/cli/satellite_onboard.py is a ~550-line byte-level clone of
dial_onboard.py with zero tests of its own, already-shipped copy drift, and
a credential-handling wart. docs/satellites.md itself leaned toward
generalizing once two device classes existed; there are two.

Branch: `codex/improv-dedup`. File fence: `jasper/cli/_improv.py` (new),
`jasper/cli/_esp32_onboard.py` (new), `jasper/cli/dial_onboard.py`,
`jasper/cli/satellite_onboard.py`, `tests/test_dial_onboard.py` (+ new
shared test file), `docs/satellites.md` (onboarding section only).

## One PR, three commits

1. **Extract the shared layer.** `jasper/cli/_improv.py`: Improv-over-Serial
   framing, `_scan_packets` (header hunting, partial-frame retention,
   checksum resync), state/error enums with the ImprovTypes.h disambiguation
   comments, `push_credentials`. `jasper/cli/_esp32_onboard.py`: the shared
   flow (USB VID/PID discovery, boot-log probe, flash decision matrix,
   cred push, mDNS wait) parameterized by a `DeviceProfile` dataclass
   (usb signature, firmware bin path, boot-log signature string, mdns
   hostname, done-message copy). `dial_onboard.py` / `satellite_onboard.py`
   become thin shims defining their DeviceProfile + argparse. CLI flags,
   help text, and per-stage exit codes (1-4) stay IDENTICAL — they're
   operator-facing contract.
2. **Fix the defects that live in the duplicated code** (each gets a test in
   the shared test file):
   - nmcli terse output keeps `\:` escapes — `split(':', 1)` corrupts
     colon-bearing SSIDs/PSKs and the failure then misleads with "running as
     root?". Unescape, or parse `nmcli -m multiline`.
   - `subprocess.run` for nmcli and esptool have no `timeout=` (wedged D-Bus
     or serial port hangs onboarding forever) — nmcli ~5 s, esptool ~120 s,
     with actionable error messages.
   - `--auto` pushes the household PSK to UNIDENTIFIED hardware after a
     failed firmware probe. Require a positive probe before pushing creds in
     --auto (mirror the retired-udev rationale documented at the top of
     jasper/web/dial_setup.py); update the help text that still cites the
     abandoned udev design.
   - Copy drift: the satellite's final message says "turning the knob will
     adjust volume" (it's a touchscreen); the boot-log signature quote
     doesn't match main.cpp exactly; redundant `except (OSError, Exception)`
     tuples. Fix all three.
3. **Tests:** parametrize the existing wire-format tests over both
   DeviceProfiles (they currently pin only the dial); add coverage for the
   new fixes (timeouts via a fake that sleeps, colon-SSID round-trip,
   --auto-refuses-without-probe). Target: satellite_onboard reaches parity
   with test_dial_onboard.py through the shared module.

Acceptance: `pytest tests/test_dial_onboard.py <new shared file> -q` green;
`ruff check .`; both CLIs' `--help` output unchanged except corrected copy
(attach before/after to PR body); docs-impact: update docs/satellites.md's
onboarding section to name the shared module; flag needs-on-device: one real
dial OR satellite onboard run by the maintainer before the next accessory
flash session.
