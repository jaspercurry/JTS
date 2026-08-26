# HANDOFF — Tier 5 watchdog liveness: the gap and what stays deferred

The kernel hardware watchdog cannot see userspace. This doc holds the two
things that outlive the fix: **why** it cannot, and the dials (T5.3–T5.5) that
were surveyed, declined, and are still declined.

What shipped — T5.1 (`StartLimitAction=reboot` on the critical units) and T5.2
(the `SystemSupervisor` probe loop), plus everything added since — is owned by
[HANDOFF-resilience.md](HANDOFF-resilience.md). Do not restate it here. The
decision and its mitigations are
[ADR-0146](adr/0146-userspace-liveness-is-two-software-layers-and-three-deferred-dials.md);
the 2026-05 incident narrative, the full five-option survey with costs, the
comparison matrix, and the source reading are archived in
[`historical/tier5-watchdog-options-2026-05.md`](historical/tier5-watchdog-options-2026-05.md).

## Why the hardware watchdog cannot represent userspace liveness

`bcm2835-wdt` patted by systemd PID 1 confirms exactly one thing: **PID 1 got
CPU inside the window.** It says nothing about sshd, jasper-control, or audio.
Four facts make that unfixable at this layer, and they are the reason the
answer had to be built above it:

- **There is no kernel-side liveness logic.** Per the
  [kernel watchdog API](https://www.kernel.org/doc/html/latest/watchdog/watchdog-api.html),
  writing to `/dev/watchdog` (`WDIOC_KEEPALIVE`, or any byte except `'V'`)
  only tells the driver to re-arm the timer. The driver does not check whether
  anything else is alive. Patting *is* the liveness signal.
- **The hardware ceiling is ~16 s**, not the configured value: `bcm2835_wdt`
  uses a 20-bit seconds-scale field, so systemd services a longer
  `RuntimeWatchdogSec` by re-patting about 4× per minute. A wedge must starve
  PID 1 for >15 s consecutively to risk a reset.
- **PID 1's loop is far cheaper than the work that wedges.** During the
  2026-05-23 incident PID 1 needed roughly **1 ms of CPU per 15 s window** to
  keep the box "alive" — trivially achievable under heavy zram thrash, while
  sshd could not complete a banner exchange.
- **This is industry-typical, not a JTS misconfiguration.** Home Assistant OS
  reproduces the identical signature under a CIFS I/O stall
  ([HAOS #4547](https://github.com/home-assistant/operating-system/issues/4547)):
  service-level `WatchdogSec` fires, no hardware reset, still open with no
  maintainer fix. balenaOS answers it with a much shorter
  `RuntimeWatchdogSec` plus per-service probe wrappers — which is T5.3 below.

Rule of thumb when reading a wedge report: if the box answered ICMP and
accepted TCP but nothing completed, the hardware watchdog was never going to
fire. Look at T5.2's probe events, not at `/dev/watchdog`.

## Still deferred, with the trigger that reopens each

These are live decisions, re-evaluated when their trigger fires — not a backlog.

### T5.3 — a tighter `RuntimeWatchdogSec` (plus healthdog-style probes)

Drop the window (balena runs 10 s; JTS runs the RPi OS default) and gate each
critical daemon's pat on a per-daemon probe.

**Why not:** the window itself is the load-bearing parameter and there is no
field data. Too tight risks spurious reboots during legitimate slow operations
— deploys, room-correction sweeps, an `apt upgrade`. balena's 10 s works
because balena runs unattended; JTS runs in a living room, where a reboot
during every install is much worse UX than a twice-yearly wedge. HAOS chose to
effectively disable the hardware watchdog rather than take this risk.

**Revisit when:** 30+ days of `/state` data under T5.1+T5.2 show wedges
persisting at more than about one per month. If they do not, T5.3 is
unnecessary rather than pending.

### T5.4 — an external hardware watchdog

A watchdog HAT that cuts power on a missed I2C heartbeat (and, on the models
worth buying, adds battery backup), or a DIY microcontroller driving a relay.

**Why not:** it changes the BOM and needs a chassis revision and sourcing,
while the software layers shipped in days.

**Revisit when:** the next hardware revision is specified. A
watchdog-plus-UPS HAT is a genuine value-add there, because it also answers the
unclean-power corruption that the software ladder cannot — but it is not a
retrofit for the current fleet.

### T5.5 — a PSI-based watchdog gate

Read `/proc/pressure/memory` and stop patting when `full avg60` crosses a
threshold, converting sustained stall into a hardware reset.

**Why not:** no production project uses PSI this way. The closest tools
(systemd-oomd, nohang, earlyoom) all use PSI to *kill a process*, never to
*escalate to reboot* — a PSI-driven reboot is far more dangerous than a
PSI-driven kill, and PSI is noisy enough that "reboot if pressured" is a poor
default. It also needs PSI enabled, which stock RPi OS does not do by default.
The gap between "the system is pressured" and "the system is dead" is too wide
to bridge with PSI alone.

**Revisit when:** a kernel exposes a more direct "userspace is dead" signal.
Being first here is not worth it.

## One composition systemd still cannot express

"If **3 of these** critical services fail their watchdog within window W,
escalate" has no native systemd form — `StartLimitAction=` is per-unit. It
could be built into the T5.2 supervisor (read the journal for service-level
watchdog events and count), and was left unbuilt as added scope. Anything
wanting cross-unit escalation should start there rather than inventing a second
mechanism.

Last verified: 2026-08-26 (triage pass — the watchdog-API and `bcm2835_wdt`
ceiling claims rechecked against their cited sources, and the T5.1/T5.2
description reduced to a pointer because `HANDOFF-resilience.md` owns it and
has moved ahead of this doc's copy. The 2026-05 incident narrative, option
survey, comparison matrix, and sources moved to
`historical/tier5-watchdog-options-2026-05.md`; the decision and its
mitigations became ADR-0146. The deferral triggers above are unchanged from
their 2026-05 statement and remain unmet.)
