# Tier 5 watchdog liveness — option survey (2026-05) — historical

> **Status: historical.** The research and option comparison that produced
> T5.1 and T5.2, frozen as written in May 2026. The decision is
> [ADR-0146](../adr/0146-userspace-liveness-is-two-software-layers-and-three-deferred-dials.md).
> Costs, thresholds, and unit lists below describe the 2026-05 tree and were
> not re-verified.

## The problem

### The 2026-05-23 incident

A large third-party toolchain compile on the 1 GB Pi 5 OOM-stalled userspace for
>2 minutes during a JTS install session:

- **ICMP ping**: healthy (~7 ms RTT, 0% loss) — kernel + network
  stack alive
- **SSH connection**: TCP accepted, **banner exchange timed out** —
  userspace too starved to complete handshake
- **No watchdog reset** — PID 1 stayed alive enough to keep
  patting `/dev/watchdog0` every <60 s
- **Required manual power-cycle** to recover

### Why Tier 5 didn't fire

Per the kernel docs at [watchdog-api.html](https://www.kernel.org/doc/html/latest/watchdog/watchdog-api.html),
patting `/dev/watchdog` (via `WDIOC_KEEPALIVE` or any byte except
`'V'`) only tells the *driver* to re-arm the hardware timer.
**There is no kernel-side liveness logic** — the driver doesn't
check "is sshd alive?" before re-arming. Patting *is* the only
liveness signal.

So the systemd PID-1-pats-`/dev/watchdog0` model translates to:
"the hardware watchdog fires iff PID 1 fails to get CPU for >60 s."
PID 1's main loop is far cheaper than userspace's work. A
zram-thrashed system can deschedule everyone but PID 1 and stay
there for minutes.

The Pi 5's `bcm2835_wdt` has a hardware ceiling of `max_hw_heartbeat_ms ≈ 16 s`
(20-bit timer field, seconds-scale) per the [driver source](https://github.com/torvalds/linux/blob/master/drivers/watchdog/bcm2835_wdt.c).
systemd handles longer `RuntimeWatchdogSec` values by re-patting
4× per minute. So patting is happening every ~15 s; a wedge has
to starve PID 1 for >15 s consecutive to risk reset. **In practice,
PID 1 needed only ~1 ms of CPU per 15 s window during the
2026-05-23 incident** to stay alive — trivially achievable even
under heavy thrash.

### Industry-typical, not idiosyncratic

[HAOS issue #4547](https://github.com/home-assistant/operating-system/issues/4547)
documents the same shape under CIFS I/O stall: kernel I/O hang,
service-level `WatchdogSec` fires for journald and timesyncd, but
**no hardware reset**. Status: open, no maintainer fix. balenaOS
addresses this with a much shorter `RuntimeWatchdogSec=10` plus
per-service [healthdog-rs](https://github.com/balena-os/healthdog-rs)
probes — see Options C and A below.

The honest framing: **single-process patting cannot represent
userspace liveness.** The fix has to live above the kernel
watchdog, not inside it.

## Options

### Option A — Probing system supervisor in `jasper-control` (recommended for T5.2)

A new `jasper/control/system_supervisor.py` running inside
`jasper-control` on a dedicated daemon thread and asyncio event loop.
**Mirrors the proven
shape of [`jasper/control/shairport_supervisor.py`](../../jasper/control/shairport_supervisor.py)**
(Tier 3, in production since the 2026-05-23 shairport-sync wedge):

```
class SystemSupervisor:
    every 30 s ± 3 s jitter:
        probe set:
            - TCP connect to 127.0.0.1:22 (sshd) within 2 s
            - HTTP GET 127.0.0.1:8780/healthz within 2 s
            - `cat /proc/loadavg` reads cleanly within 1 s
        if probe raises or times out: failures += 1
        if failures >= 3:
            if not rate_limited:    # 1 reboot per 24 hours
                log structured event=system_supervisor.escalate
                systemctl --no-block reboot
                rate_limit_set()
```

Same primitives as `ShairportSupervisor`: consecutive-failure
threshold, rate limit, and cold-start delay, all configurable. Unlike
the source-specific supervisor, this whole-system liveness gate is
deliberately not session-gated (rationale below). Run inside
`jasper-control` (already has `/state` for
observability and the watchdog supervisor pattern). No new daemon or
systemd unit.

**Engineering cost**: ~200 lines of new Python mirroring an
existing file. ~1 engineer-day with tests and review. Adds a
section to `/state` for the dashboard.

**Resource cost**: one mostly idle daemon thread and event loop inside
the existing process; no second Python interpreter. The ~30 s probe
cadence costs <1% CPU.

**Blast radius if buggy**: a false-positive reboot is the
disaster scenario. Mitigations: (a) 3-consecutive-failure
threshold (matches `ShairportSupervisor`); (b) 24-hour rate limit;
(c) 120-second cold-start window before any probe; (d)
`JASPER_SYSTEM_SUPERVISOR=disabled` env-var escape hatch
mirroring `JASPER_SHAIRPORT_SUPERVISOR=disabled`. There is no active-
session gate: three consecutive failures of any whole-system probe mean
the session cannot be trusted as live, and the durable rate limit is the
fail-safe against repeated false positives. A second-
order risk: the supervisor itself wedges, no escalation. But
that's exactly what Tier 5 (hardware watchdog) catches — they
compose.

**Reversibility**: full. The env-var off-switch flips it dead
without a redeploy. PR-revert removes the file.

**Why this matches JTS doctrine**:
- Mirrors an established in-tree pattern (Tier 3 supervisor)
- Doesn't introduce a new process or systemd unit; the bounded cost is
  one mostly idle daemon thread and event loop
- Logs structured events (`event=system_supervisor.*`) — same
  shape as existing `event=shairport.*` discipline
- Off-switchable via env file → fits the "operator escape hatch"
  doctrine documented in AGENTS.md
- Lives in `jasper-control` which already aggregates `/state`,
  so the dashboard surface is free

### Option B — `StartLimitAction=reboot-force` per critical service (recommended for T5.1, ships today)

Pure systemd composition, **zero new code**. For each critical
daemon (`jasper-camilla`, `jasper-aec-bridge`, `jasper-voice`,
`jasper-control`), add to `[Unit]`:

```ini
StartLimitIntervalSec=300
StartLimitBurst=4
StartLimitAction=reboot-force
```

Effect: if any of these services hits its `WatchdogSec=30s`
timeout 4 times within 5 minutes, systemd itself cleanly reboots
the whole box. Files unmount, journal flushes, dirty pages
sync. From the operator's view: same as a watchdog reset, but
clean — no `EXT4-fs orphan cleanup on readonly fs` in the next
boot's dmesg.

Per the systemd v228+ docs at
[systemd.unit(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html#StartLimitAction=)
and the OneUpTime writeup at
[oneuptime.com/blog/.../how-to-configure-systemd-watchdog](https://oneuptime.com/blog/post/2026-03-02-how-to-configure-systemd-watchdog-for-service-health-checks-on-ubuntu/view):
"If a service is restarted more frequently than 4 times in 5
minutes, action is taken and the system is quickly rebooted
with all file systems being clean when it comes up again."

**Engineering cost**: ~10 lines across 4 unit files. ~30 minutes
including test plan.

**Resource cost**: zero.

**Blast radius if buggy**: a critical service that genuinely
crashes 4× in 5 min triggers a reboot. That's also the
*intended* behavior — if jasper-camilla can't stay up that
many times in a row, the system needs intervention beyond
"restart it again." `reboot-force` (vs `reboot`) skips
synchronization on the assumption that the system is already
sick; for our case `reboot` (clean) is the more conservative
choice — recommend that.

**Reversibility**: full. Remove the three directives from the
unit and `systemctl daemon-reload`.

**What this catches that T5.2 doesn't**: a single critical
daemon repeatedly failing (e.g., a regression that makes
jasper-voice OOM on connect) while the rest of the system is
fine. Tier 5.2's system-wide probes would all pass; only the
specific failed service's `WatchdogSec` knows.

**What this doesn't catch that T5.2 does**: a wedge that
doesn't trigger any jasper-* `WatchdogSec` (e.g., the
2026-05-23 incident shape — the compile didn't make any
jasper-* daemon fail its watchdog; userspace was just slow
to respond to network probes).

T5.1 + T5.2 compose. Each catches a different shape of failure.

### Option C — Shorter `RuntimeWatchdogSec` + healthdog-style per-service probes

balena's posture: `RuntimeWatchdogSec=10` (vs JTS's current 60),
plus [healthdog-rs](https://github.com/balena-os/healthdog-rs)
wrapped around each critical service. healthdog only pats
`sd_notify(WATCHDOG=1)` if a probe script exits 0.

Drop our `RuntimeWatchdogSec` to 20 s (between balena's 10 and
HAOS's effective-off; tighter than 60 but not aggressive enough
to false-positive on a deploy's SD sync window). Each critical
daemon's `ExecStart=` gets prefixed with healthdog calling a
probe specific to that daemon (camilla: "websocket replies to
ping;" voice: "wake loop bumped sentinel in last 5 s;" control:
"HTTP /healthz returns 200").

**Engineering cost**: significant. healthdog-rs is Rust + cross-
compile; or we port the pattern to Python (~100 lines, runs as
a wrapper). Plus 4 per-daemon probe scripts. Plus tuning the
new `RuntimeWatchdogSec` against real production SD sync
latency (the 60 s default exists for a reason — shorter
windows risk false-positive reboots during legitimate slow ops
like `apt upgrade`).

**Resource cost**: ~5 MB per wrapped daemon (Python interpreter
overhead × 4). Or ~1 MB if Rust port.

**Why deferred**: the **tighter `RuntimeWatchdogSec` is the
load-bearing parameter and we don't have field data**. Going
to 20 s might cause spurious reboots during normal heavy
operations (deploys, room-correction sweeps, the `pio` compile
itself when run elsewhere). HAOS chose to effectively *disable*
the hardware watchdog rather than risk this. balena's 10 s
works because they run unattended; JTS runs in someone's living
room where a 30-second-startup reboot every install is much
worse UX than a once-in-six-months wedge.

**Revisit when**: we have 30+ days of `/state` data on actual
wedge frequency under T5.1 + T5.2. If wedges persist at >1/month,
T5.3 is the next dial. If they don't, T5.3 is unnecessary.

### Option D — External hardware watchdog (Sequent HAT or DIY ESP32)

Two production options:

1. [**Sequent "Super Watchdog HAT"**](https://sequentmicrosystems.com/products/super-watchdog-hat-with-battery-backup-for-raspberry-pi)
   — I2C @ 0x30. Cuts power, waits 10 s, restores. Default 120 s
   timeout. UPS battery backup as a bonus. The Pi *must* talk
   I2C to it within timeout or it cycles power. ~$50. **Solves
   two problems**: watchdog liveness AND graceful shutdown on
   power loss (relevant given the 2026-05-23 WiFi-profile
   corruption story documented in HANDOFF-resilience.md).
2. **DIY microcontroller watchdog**: a separate controller monitors
   Pi mDNS responsiveness / pings / HTTP probe, drives a relay
   on power-cycle. ~$15 in parts. This would be an independent
   hardware product with its own firmware and support burden.

**Why deferred**: software options (A + B) ship in days. Hardware
options change the BOM, require chassis revision, and depend on
sourcing. Reasonable answer to "what if A + B aren't enough":
revisit D after 90 days of production data.

The Sequent HAT specifically deserves consideration for the
*next* hardware revision — UPS backup + watchdog in one is a
real value-add. But not for the existing fleet.

### Option E — PSI-as-watchdog-gate (deferred — novel)

Read `/proc/pressure/memory` `full avg60`; if >50%, the system
is stalling enough that it's not doing useful work, so stop
patting `/dev/watchdog0` → hardware reset in 60 s.

**Why we're not doing this**: no production project I could
find uses PSI as a watchdog gate. The closest tools (systemd-
oomd, nohang, earlyoom) all use PSI to *kill processes*, not
to *escalate to reboot*. The trade-off is uncomfortable: a
PSI-driven reboot is more dangerous than a PSI-driven kill,
and PSI is noisy enough that false-positive rates make
"reboot if pressured" a poor default. Plus PSI requires
`psi=1` cmdline + `CONFIG_PSI_DEFAULT_DISABLED=y` reverted —
not yet available on stock RPi OS Trixie kernels we ship on.

If a future kernel exposes a more direct "userspace is dead"
signal, revisit. For now, the gap between "system is pressured"
and "system is dead" is too big to bridge with PSI alone.

## Comparison matrix

| Option | Eng cost | Resource cost | Reversibility | Catches 2026-05-23 shape? | Catches "one daemon stuck" shape? |
|---|---|---|---|---|---|
| **A. System supervisor** | 1 day | ~0 (in-process) | env-var off-switch | ✅ — system-wide probes catch userspace death | partial — only via aggregate effect |
| **B. `StartLimitAction=reboot-force`** | 1 hour | 0 | unit-file revert | ❌ (no jasper-* failed) | ✅ — exactly this pattern |
| **C. Shorter `RuntimeWatchdogSec` + healthdog** | 3+ days | ~5 MB | yes | ✅ — tighter window catches PID 1 stutter | partial |
| **D. External HW watchdog (Sequent)** | hours of dev + $50 BOM | ~0 | hardware reversal | ✅ — bulletproof | ✅ |
| **E. PSI-as-watchdog-gate** | 2 days | ~0 | env-var | indirect (PSI proxy) | ❌ |

## Recommendation

**Ship T5.1 (Option B) immediately** — it's zero-code, zero-risk,
catches the failure mode where a single critical daemon is the
problem, and composes with everything else. Lines of work:

```ini
# Drop-in for each of jasper-camilla, jasper-aec-bridge,
# jasper-voice, jasper-control:
[Unit]
StartLimitIntervalSec=300
StartLimitBurst=4
StartLimitAction=reboot   # not reboot-force — we want clean unmount
```

Note: use `reboot` (clean), not `reboot-force`. `reboot-force`
skips sync; on a 1 GB Pi with potentially-dirty pages in zram,
that's the wrong default.

**Schedule T5.2 (Option A) for the following sprint** — one
focused day of work. Build it in `jasper-control` mirroring
`ShairportSupervisor` exactly. Probe set:
- TCP connect 127.0.0.1:22 (sshd accepting)
- HTTP GET 127.0.0.1:8780/healthz (control alive — yes, the
  supervisor probes itself, which catches the "we're hung in
  asyncio" case). A `429` from jasper-control's request-admission
  gate counts as alive-but-shedding, not dead.
- `cat /proc/loadavg` reads in <1 s (catches kernel I/O stall)

Threshold: 3 consecutive failures, 30 s cadence with ±3 s
jitter. Rate-limit: 1 reboot per 24 hours. There is deliberately no
active-session gate; see the rationale in the implementation and above.
Off-switch: `JASPER_SYSTEM_SUPERVISOR=disabled`
in `/etc/jasper/jasper.env`.

**Don't ship T5.3 (Option C) yet** — needs 30 days of production
data on wedge frequency to justify the tighter window.

**Don't ship T5.4 (Option D) for the current hardware revision** —
revisit for v2 hardware spec with UPS backup as a value-add.

**Don't ship T5.5 (Option E)** — no precedent, high false-positive
risk, not worth being first.

## Open questions / what I couldn't verify

- **Exact current `RuntimeWatchdogSec` on the deployed jts2.local** —
  we know it's RPi OS Trixie's default of 60 s, but a custom
  drop-in could override. Verify with `systemctl show
  --value -p RuntimeWatchdogUSec` before assuming.
- **Whether `bcm2712_wdt` (Pi 5 native) allows longer timeouts
  than `bcm2835_wdt`** — both architecturally use the same 20-bit
  field per the driver source, but I didn't test on hardware.
- **Whether systemd-oomd works on Pi 5 with kernel 6.12.3-2** —
  the [issue #5933](https://github.com/raspberrypi/linux/issues/5933)
  fix claim is unverified for current Trixie; relevant for
  whether Stage 3 of the memory plan can use oomd.
- **A canonical multi-service `StartLimitAction` composition** —
  "if 3 of these critical services fail their watchdog within
  W, escalate" doesn't exist natively in systemd. Could be built
  as part of T5.2 (the system supervisor reads journal for
  service-level watchdog events and counts), but adds scope.

## Sources

- Kernel watchdog API — https://www.kernel.org/doc/html/latest/watchdog/watchdog-api.html
- `bcm2835_wdt` driver source — https://github.com/torvalds/linux/blob/master/drivers/watchdog/bcm2835_wdt.c
- systemd `RuntimeWatchdogSec`, `RebootWatchdogSec`, `KExecWatchdogSec` — https://manpages.debian.org/testing/systemd/systemd-system.conf.5.en.html
- systemd `StartLimitAction=` — https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html#StartLimitAction=
- Meskes `watchdog(8)` — https://manpages.debian.org/testing/watchdog/watchdog.conf.5.en.html
- balena healthdog-rs — https://github.com/balena-os/healthdog-rs
- balena watchdog blog — https://blog.balena.io/keeping-your-system-running-watchdog/
- balena founder on HN — https://news.ycombinator.com/item?id=21275653
- HAOS issue #4547 (same gap) — https://github.com/home-assistant/operating-system/issues/4547
- Pi 5 freeze report — https://github.com/raspberrypi/linux/issues/7184
- Pi 5 watchdog driver fallback — https://github.com/raspberrypi/linux/issues/6921
- OneUpTime systemd watchdog guide — https://oneuptime.com/blog/post/2026-03-02-how-to-configure-systemd-watchdog-for-service-health-checks-on-ubuntu/view
- Lennart on systemd watchdog — http://0pointer.de/blog/projects/watchdog.html
- troglobit/watchdogd — https://github.com/troglobit/watchdogd
- PSI kernel docs — https://docs.kernel.org/accounting/psi.html
- psi-notify — https://github.com/cdown/psi-notify
- Chris Down on PSI + oomd — https://chrisdown.name/2020/05/06/psi-notify-notifying-before-cpu-memory-io-becomes-oversaturated.html
- Sequent Super Watchdog HAT — https://sequentmicrosystems.com/products/super-watchdog-hat-with-battery-backup-for-raspberry-pi
- In-tree pattern: `jasper/control/shairport_supervisor.py` (Tier 3 reference)
- In-tree pattern: `jasper/watchdog.py` (Tier 1 progress-sentinel reference)


---

Frozen 2026-08-26 with its 2026-05 content (the option matrix was never fully
re-reviewed after T5.1/T5.2 shipped, which is why it is archived rather than
maintained).
