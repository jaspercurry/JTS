# ADR-0146: Userspace liveness is two software layers, and three deferred dials

- **Date:** 2026-08-26
- **Status:** Accepted (T5.1 and T5.2 shipped 2026-05-24; recorded here when
  HANDOFF-tier5-watchdog-liveness.md was trimmed to its operational spine)

## Context

The kernel hardware watchdog patted by PID 1 confirms exactly one thing: that
PID 1 got CPU inside the window. It cannot confirm sshd accepts connections,
that jasper-control answers HTTP, or that audio is moving. On 2026-05-23 a
large compile OOM-stalled userspace for over two minutes: ICMP healthy, TCP
accepted but the SSH banner never arrived, **no watchdog reset**, manual power
cycle required. PID 1 needed roughly 1 ms of CPU per pat window to keep the
box "alive".

This is structurally inherent to single-process patting, not a misconfiguration
— Home Assistant OS hits the identical shape under a CIFS I/O stall
([HAOS #4547](https://github.com/home-assistant/operating-system/issues/4547),
still open). The fix has to live above the kernel watchdog.

Five options were surveyed (archived in
[`historical/tier5-watchdog-options-2026-05.md`](../historical/tier5-watchdog-options-2026-05.md)).

## Decision

**Two software layers ship and compose; three dials stay deferred with named
revisit triggers.**

- **T5.1 — `StartLimitAction=reboot`** on the critical jasper-* units. Pure
  systemd composition, no new code. Catches "one critical daemon is broken".
  `reboot`, never `reboot-force`: a 1 GB Pi with dirty zram pages needs the
  clean shutdown.
- **T5.2 — an in-process probing supervisor** (`SystemSupervisor` in
  jasper-control), mirroring the proven Tier 3 `ShairportSupervisor` shape
  rather than introducing a new daemon or unit. Catches "the whole box is
  wedged", which no jasper-* `WatchdogSec` would have noticed on 2026-05-23.
- The kernel watchdog stays as the floor, and catches the supervisor itself
  wedging.

Deferred, each with the condition that would reopen it:

- **T5.3, a tighter `RuntimeWatchdogSec`** — the window is the load-bearing
  parameter and there is no field data. balena runs 10 s because it runs
  unattended; HAOS effectively disabled the watchdog rather than risk spurious
  reboots. A living-room speaker rebooting during a deploy is worse UX than a
  twice-yearly wedge. **Revisit** after 30+ days of `/state` wedge data under
  T5.1+T5.2 showing >1 wedge/month.
- **T5.4, an external hardware watchdog** — changes the BOM and the chassis.
  **Revisit** for the next hardware revision, where a watchdog-plus-UPS HAT
  also answers unclean-power corruption.
- **T5.5, a PSI-as-watchdog gate** — no production precedent anywhere; the
  comparable tools (systemd-oomd, nohang, earlyoom) use PSI to *kill*, never to
  *reboot*, and stock RPi OS does not enable PSI by default. **Revisit** only
  if a kernel exposes a more direct "userspace is dead" signal.

## Consequences

- A false-positive reboot is the disaster case, so the supervisor carries four
  independent mitigations: a 3-consecutive-failure threshold, a 24 h rate limit
  persisted across the reboot it issues, a cold-start window before the first
  probe, and an env-var off-switch.
- **No active-session gate**, deliberately: three consecutive failures of a
  whole-system probe mean the session cannot be trusted as live, and the
  durable rate limit is the fail-safe against repeated false positives.
- The supervisor probes **its own** `/healthz`, which is what catches a wedged
  asyncio loop that systemd still considers healthy. A `429` from the
  request-admission gate counts as alive-but-shedding — treating overload
  shedding as a failed liveness probe would let a LAN request burst manufacture
  a reboot.
- Both layers are fully reversible: a unit-file revert and an env-var flip.
- Operational truth for what shipped lives in
  HANDOFF-resilience.md (deleted per ADR-0199), not in the design doc that
  proposed it — including the bootloop guard that later bounded T5.1's
  otherwise unbounded across-boot escalation.
