# Resilience — design, rationale, current state

This document captures the architectural decisions behind JTS's
failure-recovery design, the production incident that drove them,
and what is wired vs. what is deferred. It pairs with the
implementation in `jasper/watchdog.py`,
`deploy/systemd/jasper-aec-bridge.service`, and
`deploy/systemd/jasper-voice.service`.

The goal: the speaker runs unattended in someone's home for years
and recovers from any failure without human intervention.

---

## The 2026-05-11 incident

The motivating failure, summarised so future readers don't have to
reconstruct it from PR descriptions.

What happened: `jasper-aec-bridge`'s mic-side PortAudio
`InputStream` stopped invoking its callback after a USB underrun
on the XVF chip's UAC2 capture endpoint. The bridge's main thread
was blocked in `out_stream.write()` (PortAudio writing to the old
`hw:LoopbackAEC,0` snd-aloop card) and never observed Python's
`SIGTERM` handler because signal handlers only run between Python
bytecodes — a blocked C call holding the GIL is opaque to them.

`systemd` waited the default 90 s `TimeoutStopSec`, then sent
`SIGKILL`. The SIGKILL killed the bridge mid-flight while it held
open the snd-aloop loopback fd. snd-aloop's kernel-side
`loopback_cable` struct ([sound/drivers/aloop.c](https://github.com/torvalds/linux/blob/master/sound/drivers/aloop.c))
was left in a half-bound state: the kernel timer that advances
`hw_ptr` never re-armed. Every fresh bridge process opened
`hw:LoopbackAEC,0` successfully but blocked on its second write
forever, because the kernel-side transfer to the capture pair
was wedged.

Only `rmmod snd_aloop && modprobe snd_aloop` (after stopping all
six snd-aloop consumers — `shairport-sync`, `librespot`,
`bluealsa-aplay`, `jasper-camilla`, `jasper-aec-bridge`,
`jasper-voice`) or a full reboot recovered the kernel state.
The wake-word path was silently dead for ~10 minutes before we
noticed; no audible cue fired because cues are gated on a wake
event firing, and wake events require mic input we didn't have.

Three classes of fragility composed into the incident:

1. **PortAudio `InputStream` is one-shot** when the underlying
   ALSA PCM hits `SND_PCM_STATE_DISCONNECTED`. `snd_pcm_recover()`
   does not recover that state per the ALSA contract; there's no
   in-process retry.
   *(Detecting this stall is two-pronged as of 2026-05-31: the
   original continuous-empty counter — `JASPER_AEC_STALL_RESTART_SEC`,
   5 s — plus a slow-drip frame-rate watchdog,
   `JASPER_AEC_STALL_DRIP_MAX_WINDOWS`, that catches an intermittent
   trickle the continuous counter keeps resetting through. The first
   never fired during a ~13 h deaf-but-trickling episode; the rate
   watchdog closes that gap. Both raise `BridgeStalled` → systemd
   restart; see `_MicStarvationWatchdog` in `jasper/cli/aec_bridge.py`.)*
2. **Blocking I/O in a Python daemon defeats `SIGTERM`** — the
   GIL + bytecode-boundary signal-handler model means a blocked C
   call cannot be interrupted by Python's `signal.signal` handler.
3. **`SIGKILL` of an snd-aloop consumer corrupts kernel state**
   that survives process restarts. This is structural in aloop's
   design (the cable struct is module-global and assumes
   cooperative close).

---

## What we adopted

Two architectural changes, layered. The pattern is
[Crash-Only Software (Candea & Fox, HotOS-IX 2003)](https://www.usenix.org/legacy/events/hotos03/tech/full_papers/candea/candea.pdf):
design every component so the only stop path is crash-and-recover,
exercise that path constantly, prefer micro-reboots over full
reboots.

### 1. Eliminate the kernel-state failure class structurally — UDP transport

The bridge→voice path no longer uses snd-aloop. The bridge sends
AEC'd mono int16 frames over UDP localhost
(`127.0.0.1:JASPER_AEC_UDP_PORT`, default 9876) using a
non-blocking `socket.SOCK_DGRAM`; `jasper-voice`'s `UdpMicCapture`
(`jasper/audio_io.py`) binds the same port via
`asyncio.DatagramProtocol` and yields the same 1280-sample frames
that `MicCapture` does.

Why UDP beats hardening snd-aloop:

- **No kernel-side state to corrupt.** Either side can crash
  without affecting the other. No module to reload, no consumer
  ordering to enforce.
- **`sendto()` is non-blocking on `lo`** at our rate
  (~256 kbps). The bridge's main thread can always observe
  `SIGTERM` and exit inside the 5 s `TimeoutStopSec` — no more
  `SIGKILL` cascade.
- **Standard pattern.** Mumble, every VoIP gateway, Snapcast.
  UDP localhost packet loss is effectively zero on Linux's `lo`
  at this rate.
- **Same frame contract.** `UdpMicCapture.OUTPUT_FRAME_SAMPLES ==
  MicCapture.OUTPUT_FRAME_SAMPLES`; voice's `WakeLoop` is
  transport-agnostic.

The music-side snd-aloop card (`Loopback`, card 6) stays —
CamillaDSP is a well-behaved C++ daemon that handles `SIGTERM`
correctly and never gets `SIGKILL`'d, so its loopback never
wedges. We removed only the second card (`LoopbackAEC`) from
`/etc/modprobe.d/snd-aloop.conf`.

### 2. A five-tier resilience ladder, with sd_notify watchdog as Tier 1+2

> **Note**: the 5-tier ladder below is the original 2026-05-11
> response, scoped to **liveness failures** — stuck processes,
> wedged supervisors, hung subsystems. Two later additions extend
> the ladder, both shipped in May 2026:
>
> - **Stage 1 — memory-pressure prevention** sits *parallel* to
>   the ladder. See [Memory-pressure resilience (Stage 1)](#memory-pressure-resilience-stage-1)
>   below.
> - **T5.1 + T5.2** sit *below* Tier 5, catching the "userspace
>   dead but PID 1 alive" shape the hardware watchdog
>   structurally misses. See [Tier 5's liveness blind spot](#tier-5s-liveness-blind-spot--known-gap)
>   for the gap analysis and
>   [HANDOFF-tier5-watchdog-liveness.md](HANDOFF-tier5-watchdog-liveness.md)
>   for the option matrix + revisit triggers on the still-deferred
>   T5.3–T5.5 options.

Even with the snd-aloop failure class eliminated, we want
recovery from *any* future in-process hang — not just the
specific one we hit. The systemd `sd_notify` watchdog gives us
that generically.

| Tier | Mechanism | Catches | Wired? |
|---|---|---|---|
| 1 | `sdnotify` heartbeat thread with progress sentinel | Logic deadlock; blocked event loop; slow loop. `bump()` is called from each successful frame; the heartbeat thread only pats systemd if `now - last_progress < 5 s`, so a wedged loop stops patting even though the heartbeat thread itself keeps running. | ✅ — `jasper/watchdog.py`, wired into bridge `_aec_loop` and voice `WakeLoop.run` |
| 2 | systemd `Type=notify` + `WatchdogSec=30s` + `Restart=on-watchdog` + `TimeoutStopSec=5s` + `StartLimitBurst=20` | Process exit, hang, fatal ALSA error. If the Tier 1 heartbeat stops patting, this fires at 30 s and brings the daemon back with a fresh process in ~2 s. | ✅ — `deploy/systemd/jasper-aec-bridge.service`, `deploy/systemd/jasper-voice.service` |
| 3 | Sidecar protocol-level liveness probe in `jasper-control` + conditional `systemctl restart`, gated on no active session and rate-limited | Third-party daemons that wedge at the protocol layer while still passing systemd's liveness check. shairport-sync AP2 today; pattern generalizes to other long-lived third-party renderers if they demonstrate the same failure class. | ✅ — `jasper/control/shairport_supervisor.py`, started from `server.py:main` via `start_supervisor()` |
| 4 | Kernel-state recovery script (`rmmod && modprobe snd_aloop`, after stopping all consumers; rate-limited via state file) | snd-aloop kernel-side wedges, dsnoop wedges. | ❌ — not currently wired. The original motivation (bridge↔voice snd-aloop) is gone; the music-chain Loopback hasn't shown this failure mode in production. Deferred. |
| 5 | BCM2712 hardware watchdog (`/dev/watchdog0`) patted by systemd PID 1 via `RuntimeWatchdogSec=1m` (with persistent journald for post-mortem forensics) | Kernel panic, PID 1 hang, total userspace wedge (CPU peg, swap thrash, I/O hung). | ✅ — wired by Raspberry Pi OS Trixie's `/usr/lib/systemd/system.conf.d/40-rpi-enable-watchdog.conf` (`RuntimeWatchdogSec=1m`, `RebootWatchdogSec=2m`). JTS contributes the other half: PR #160 (2026-05-20) overrode the paired RPi OS default of `Storage=volatile` so logs survive the reset and the cause is debuggable. |

The honest framing: today's shipped resilience is **Tier 1, Tier 2, Tier 3 (shairport-sync only), Tier 5 (hardware watchdog with persistent journal forensics), and the architectural choice that obviated kernel-state recovery for the AEC path.** Tier 4 stays on the deferred list with a clear trigger ("rmmod + modprobe if snd-aloop ever wedges again").

### 3. Wire third-party daemons into the ladder — protocol-level supervisor

Tiers 1+2 catch one failure class exceptionally well: **liveness of
daemons we own**. We control the source, we add the sd_notify
heartbeat, the work loop bumps it, systemd kills and restarts when it
stops.

What that ladder doesn't catch: a third-party daemon that wedges at
the protocol layer while still passing every systemd liveness check.
The motivating example, observed in production 2026-05-19:
shairport-sync v4.3.7's AP2 control plane occasionally hangs after
`accept()` on a per-connection RTSP handshake. The process stays
alive, mDNS still advertises the AirPlay service, MPRIS still answers
`PlaybackStatus`, and systemd sees nothing to restart. From the
user's vantage point, "JTS" appears in the AirPlay picker but every
new SETUP times out. The only manual fix has been
[`scripts/airplay-reset.sh`](../scripts/airplay-reset.sh).

The closest upstream report is
[shairport-sync#2024](https://github.com/mikebrady/shairport-sync/issues/2024),
where `strace` showed the listener thread stuck in `pselect6`. Issue
closed without a code fix. Restarting the unit resolves it.

The supervisor at
[`jasper/control/shairport_supervisor.py`](../jasper/control/shairport_supervisor.py)
adds Tier 3: a single async coroutine running on its own thread +
asyncio loop inside `jasper-control` (same shape as
`start_peering_daemon_if_enabled`). Every 30 s ± 3 s jitter it opens
a TCP connection to `127.0.0.1:7000`, sends a minimal RFC 2326
`OPTIONS *` request, and expects `RTSP/1.0 200` within 3 s. After
3 consecutive failures, gated on MPRIS `PlaybackStatus != "Playing"`,
it issues `systemctl reset-failed + --no-block restart` on
`shairport-sync.service` and `nqptp.service` — the same units the
manual fix already touches.

Design constraints the supervisor satisfies:

- **No new long-running process.** The supervisor is one async task
  on infrastructure jasper-control already owns. Pss cost ≈ 0;
  restart latency unchanged.
- **The probe doesn't disturb a live session.** shairport's
  `handle_options_2` returns 200 OK pre-pair, in a fresh
  per-connection thread, independent of any in-flight `principal_conn`.
- **The gate is the load-bearing safety net.** Probe failure during
  a real listening session is more likely a hiccup than a wedge;
  the gate keeps us from kicking the user.
- **Rate limit prevents storms.** One supervisor-driven restart per
  10 minutes. If the wedge persists past that, the underlying issue
  is upstream and our restart isn't the right hammer.
- **Failure modes degrade safely.** A probe exception is counted as
  a probe failure (the wedge signature is "no response"; a Python
  exception in our probe code is no better). A gate exception fails
  safe to "active" (better to leave a possibly-live session alone
  than risk killing one on a transient DBus stall).
- **Off switch.** `JASPER_SHAIRPORT_SUPERVISOR=disabled` in
  `/etc/jasper/jasper.env` parks the thread before it starts.
  Exact match (case-insensitive); other values, including `off` /
  `0` / `no`, log a warning and proceed as `auto`.
- **Observable.** Structured `event=shairport.*` log lines for every
  state transition; supervisor state surfaces in the `/state` JSON
  under `resilience.shairport`.

What this Tier 3 instance is NOT designed to handle:

- An MPRIS-says-Playing-but-RTSP-wedged inconsistency. The gate is
  conservative; in that very rare state the user gets silence and
  the fix is the `/system/restart/audio` button. A secondary
  detector based on `nqptp` shm `local_time` stagnation could close
  this gap; deferred until observed.
- A wedge in nqptp independently of shairport. The restart action
  bundles both units because the manual fix has always done so, but
  the detector only probes shairport's RTSP.

The same probe → gate → rate-limited restart shape generalizes to
other third-party daemons we depend on (`librespot`, `bluez-alsa`).
None have demonstrated this failure class. The pattern is here if
they do — don't preemptively spread.

### 4. Tier 5: hardware watchdog with persistent journal forensics

The kernel hardware watchdog (`bcm2835-wdt` on the Pi 5's
BCM2712 SoC) was already enabled before JTS existed: Raspberry Pi
OS Trixie ships
[`/usr/lib/systemd/system.conf.d/40-rpi-enable-watchdog.conf`](https://github.com/RPi-Distro/repo/blob/master/debian/changelog)
with `RuntimeWatchdogSec=1m` and `RebootWatchdogSec=2m`. systemd
PID 1 opens `/dev/watchdog0`, sets the kernel timer to 60 s, and
pings every 30 s. If PID 1 itself can't get scheduled to ping
within the window — which happens when userspace wedges hard
enough to starve the scheduler (heavy zram thrash during OOM,
massive I/O queue, an in-process deadlock that radiates outward) —
the hardware watchdog hard-resets the board.

For a smart speaker that runs unattended for years, this is the
right behaviour: a wedged box recovers in ~60 s without a human
plugging it. The user perceives "the speaker restarted on its
own" — accurate, and far better than a permanently silent speaker.

The cost we discovered on 2026-05-20: RPi OS also ships
[`/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`](https://github.com/RPi-Distro/repo)
with `Storage=volatile`, which throws the journal away on every
reboot to protect the SD card from log-write wear. The two
defaults compose into a debuggability hole: the wedge → watchdog
reset → fresh boot → no record of what wedged the system. From
the operator's vantage, the speaker spontaneously reboots with
no explanation.

[PR #160](https://github.com/jaspercurry/JTS/pull/160) added
`deploy/journald/50-jts-persistent-storage.conf` (installed at
`/etc/systemd/journald.conf.d/`) flipping back to
`Storage=persistent` with a 200 MB `SystemMaxUse=` cap. Now the
previous boot's logs survive the reset and the cause is
recoverable via `journalctl -b -1`. SD wear cost: ~30 MB/hour
to disk with ZSTD compression, ~270 GB/year, well inside the
endurance budget of any reasonable SD card (~100 TBW). Swap is
on `zram0` (compressed RAM via Trixie's `zram-tools` default),
not the SD card, so OOM events don't actually thrash the card —
the wear protection RPi OS's volatile default was hedging
against turned out to be the wrong threat for our topology.

**Disk-pressure observability.** A filling root filesystem is the
slow-burn companion to SD-card *wear*: an unclean power-cut on a full
card is the corruption hazard the whole ladder exists to survive, yet
nothing surfaced it before a write failed. `/state.resilience.disk`
(`jasper/control/state_aggregate.py:_disk_snapshot`) is the
always-visible dashboard number — `{path, percent_used, free_gib,
total_gib}`, fail-soft (`null` on a non-POSIX host or statvfs error,
like every other resilience section). The actionable warn (≥85%) /
fail (≥95%) thresholds are owned by jasper-doctor's `check_disk_space`
(`jasper/cli/doctor/memory.py`), keeping the dashboard number and the
graded check from drifting.

What this Tier covers that Tiers 1–4 don't:
- Kernel panic (Tiers 1–2 require PID 1 to still be scheduling
  the heartbeat thread).
- PID 1 itself wedging (no userspace watchdog can save you when
  PID 1 is the one stuck).
- OOM-induced full-system stalls where every process — including
  systemd — is blocked waiting on zram compression or swap I/O.
- Any future failure class we haven't anticipated. Tier 5 is the
  catch-all.

What it explicitly does NOT replace: Tiers 1–4 still catch
in-process hangs and protocol wedges much faster (~30 s vs ~60 s)
and with a smaller blast radius (one daemon restart vs full
reboot). Tier 5 is the floor, not the first line.

To investigate a watchdog-triggered reset after the fact:

```sh
# How many boots are in the persistent journal?
sudo journalctl --list-boots

# Last warning+ from the previous boot, the 2 minutes before the
# reset (usually where the wedge signature appears: OOM-kill,
# softlockup, runaway daemon, etc.)
sudo journalctl -b -1 -p warning --since "-2min"

# EXT4 boot fingerprint: an unclean shutdown shows up in dmesg as
# "EXT4-fs (mmcblk0p2): orphan cleanup on readonly fs" on the
# *recovery* boot. Diagnostic shorthand for "the previous shutdown
# wasn't clean" → power loss, hardware reset, OR watchdog bite.
sudo dmesg -T | grep "orphan cleanup"
```

Heavy *offline* analysis on the Pi (e.g. instantiating
`openwakeword.Model()` 100 times in a sweep script) is a known
way to trip Tier 5 self-inflicted — each model load holds
~100–200 MB and they don't free until the script does. Prefer
the laptop for that kind of work; the Pi venv is sized for
production daemons, not analysis bursts.

### Tier 5's liveness blind spot — known gap

The 2026-05-23 incident exposed a real limitation. A PIO compile
on the 1 GB Pi 5 OOM-stalled userspace for >2 minutes:

- ICMP ping stayed healthy (~7 ms RTT, 0% loss) — kernel and
  network stack alive
- `ssh` connection accepted at TCP layer but **banner exchange
  timed out** — userspace was effectively dead
- **No watchdog reset** — PID 1 got just enough scheduler time to
  keep patting `/dev/watchdog0` every <60 s
- Required a manual power-cycle to recover

The gap: **systemd patting `/dev/watchdog0` is a very weak
liveness signal**. It only confirms PID 1's main loop got CPU
once in the last 60 s. It does not confirm that sshd accepts
connections, that jasper-control answers HTTP, that camilladsp
is processing audio, or that any user-visible service does
anything useful. So userspace can be fully wedged while Tier 5
thinks the system is healthy.

**Design proposal**: [`HANDOFF-tier5-watchdog-liveness.md`](HANDOFF-tier5-watchdog-liveness.md)
(2026-05-24). Two-PR sequence:

- **T5.1** ✅ **shipped**: `StartLimitAction=reboot` on the critical
  jasper-* units (outputd, camilla, aec-bridge, voice, control). When any
  one of them exceeds its `StartLimitBurst=` within `StartLimitIntervalSec=`,
  systemd itself cleanly reboots the box — filesystems unmount,
  journal flushes, dirty pages sync. Per-unit thresholds preserve
  existing transient-tolerance for audio-device dropouts
  (jasper-voice keeps 20/300, jasper-camilla 5/60, aec-bridge and
  control use proposal default 4/300). `reboot` not `reboot-force`
  — clean shutdown is essential on a 1 GB Pi to flush zram dirty
  pages. Catches the "one critical daemon is sick" shape, but NOT
  the "userspace is dead while jasper-* daemons happen to be alive"
  shape. Since 2026-06-02, `jasper-voice`'s first-time unconfigured
  provider exit is explicitly excluded from this budget via
  `SuccessExitStatus=78` + `RestartPreventExitStatus=78`; actual
  voice crashes still flow through T5.1. `jasper-doctor`'s
  `check_start_limit_action` surfaces
  drift if a Debian/RPi-OS update removes the directive.
  **T5.1 circuit breaker** (2026-06-10): `StartLimitAction=reboot`
  alone is unbounded across boots — a *permanent* daemon failure
  (corrupt config, dead binary) would reboot the Pi every ~2-5
  minutes forever. `jasper-bootloop-guard.service`
  ([`deploy/bin/jasper-bootloop-guard`](../deploy/bin/jasper-bootloop-guard),
  pure-bash oneshot mirroring the wifi-guardian shape, ordered
  `Before=` the escalating units) persists boot timestamps to
  `/var/lib/jasper/bootloop_guard_boots`; on the 3rd boot inside a
  3600 s window it writes **runtime** drop-ins
  (`/run/systemd/system/<unit>.d/90-jts-bootloop-guard.conf`,
  `StartLimitAction=none`). The drop-in changes only the escalation,
  not the rate limit: once the sick unit exhausts its
  `StartLimitBurst`, systemd parks it failed (visible in
  `systemctl`/`jasper-doctor`) instead of rebooting, and the Pi stays
  reachable. Operator recovery: fix the cause, then
  `systemctl reset-failed <unit> && systemctl start <unit>` (or
  reboot). Drop-ins live in `/run`, so a
  healthy boot self-re-arms the ladder with zero operator action.
  Guarded units are discovered dynamically by grepping
  `StartLimitAction=reboot`; fail-open on every error path.
  Observability: `event=bootloop_guard.ok|tripped|error` +
  `/state.resilience.bootloop_guard`.
  **Hardware-validated 2026-06-11** on the jts3 lab Pi: synthetic
  2-boot history tripped the guard on the next boot (6/6 runtime
  drop-ins, `event=bootloop_guard.tripped`, doctor WARN, control
  plane stayed up), and a clean history re-armed it on the boot
  after (0 drop-ins, `event=bootloop_guard.ok`, doctor green) —
  evidence in the
  [PR #573 execution comment](https://github.com/jaspercurry/JTS/pull/573#issuecomment-4683638459);
  runbook archived at
  [historical/RUNBOOK-2026-06-10-batch-hardware-validation.md](historical/RUNBOOK-2026-06-10-batch-hardware-validation.md).
- **T5.2** ✅ **shipped**: new `SystemSupervisor` in
  [`jasper/control/system_supervisor.py`](../jasper/control/system_supervisor.py)
  mirroring the proven `ShairportSupervisor` Tier 3 shape. Probes
  three layers every 30 s ± jitter:
    1. **sshd banner exchange** on `127.0.0.1:22` (TCP accept + SSH-
       protocol banner read within 2 s — the 2026-05-23 shape was
       sshd accepting the TCP connect but not writing the banner)
    2. **jasper-control's own `/healthz`** on `127.0.0.1:8780`
       (yes, we probe ourselves; this catches "asyncio loop wedged
       but systemd thinks we're alive")
    3. **`/proc/loadavg` read** within 1 s (kernel I/O stall)
  After 3 consecutive failures (any probe), rate-limited at 1
  reboot per 24 hours, calls `systemctl --no-block reboot` for
  a clean shutdown. The rate-limit window is enforced against a
  WALL-CLOCK last-reboot timestamp persisted to
  `/var/lib/jasper/system_supervisor_reboot.json` (loaded on
  construction, fail-open on a missing/corrupt file), so the window
  survives the reboot it just issued — otherwise a *permanent*
  userspace wedge would reboot-loop roughly every cold-start window
  (~3.5 min) forever and the household could never reach jts.local.
  Off via `JASPER_SYSTEM_SUPERVISOR=disabled`.
  Surfaced on `/state` under `resilience.system_supervisor` and
  via structured `event=system_supervisor.*` journal lines.
  `jasper-doctor` surfaces the persisted state file too
  (`supervisor reboot state`): missing → ok (never rebooted),
  corrupt or future-dated beyond NTP skew → warn, since both are
  silent fail-open at runtime.

**Stage 1 + T5.1 + T5.2 now together cover the 2026-05-23 incident
shape end-to-end**:
  - Stage 1's MGLRU + OOMScoreAdjust + sysctls reduce *frequency*
    of wedges
  - T5.1's `StartLimitAction=reboot` catches the "one critical
    daemon is broken" sub-shape
  - T5.2's `SystemSupervisor` catches the "userspace dead but
    no daemon technically failed" shape — the exact 2026-05-23
    signature

The Tier 5 kernel hardware watchdog stays as the floor for the
case where T5.2 itself wedges.

### Memory-pressure resilience (Stage 1)

Added 2026-05-24 in response to the 2026-05-23 wedge. Stage 1
ships the layer that works on the stock RPi 5 kernel without
enabling the memory cgroup controller (which is disabled by
the Pi 5 DTB — see [raspberrypi/linux#5933](https://github.com/raspberrypi/linux/issues/5933)
and [#6980](https://github.com/raspberrypi/linux/issues/6980)).
Stages 2 (cgroup-memory + slice architecture) and 3 (userspace
OOM killer + observability) are planned follow-ups.

**Layer 1a — `OOMScoreAdjust` ladder on critical daemons.** Per
`systemd.exec(5)`, each unit can request a kernel-side bias on
the OOM killer's victim selection. Values are added to
`/proc/$pid/oom_score`, so the killer becomes much less likely
to pick a daemon we've protected when memory tightens. The
JTS ladder, descending priority:

| Daemon | OOMScoreAdjust | Rationale |
|---|---|---|
| `jasper-camilla` | -900 | Silence is the worst possible UX |
| `jasper-aec-bridge` | -700 | Real-time mic processing |
| `jasper-control` | -600 | Recovery surface (operator can't reach /system/ without it) |
| `jasper-voice` | -500 | Largest blast radius (~150 MB Pss; bound by Stage 2's MemoryMax once cgroup memory lands) |
| `jasper-mux`, `jasper-input` | -300 | Restartable control-plane daemons; mux outage is now user-visible because fan-in starts safe/closed until mux selects a lane |
| `sshd` | -250 | Recovery path; moderately protected, but SSH-launched diagnostics stay killable |

Critical: **nothing operator-launched through SSH should inherit
-1000** because that fully disables OOM-kill for that PID. This was
validated by the 2026-05-28 OOM-reset investigation: a root
`python -` launched over SSH inherited the old `sshd=-1000` bias and
survived while product daemons were killed around it. -900 is
"almost never picked" for the most important product daemon;
-1000 is "literally never picked" and reserved for true system
infrastructure, not arbitrary diagnostics.

Open-ended Pi-side diagnostic work goes through
[`scripts/pi-run-diagnostic.sh`](../scripts/pi-run-diagnostic.sh),
which wraps `systemd-run` with memory/runtime bounds and a positive
`OOMScoreAdjust` so the kernel kills the diagnostic before the
speaker.

Works today on the stock kernel via `/proc/PID/oom_score_adj`
— independent of the `cgroup_disable=memory` situation. Drift
detection via `jasper-doctor`'s `check_oom_score_adj`.

**Layer 1b — Zram resized via `rpi-swap` drop-in
(`/etc/rpi/swap.conf.d/50-jts.conf`).** The Trixie default puts
zram at ~100% of RAM. On a 1 GB Pi this amplifies thrash:
the more compressed RAM is sitting in zram, the more zsmalloc
bookkeeping has to compete with the workload for CPU during
reclaim. Modern best practice ([Fedora SwapOnZRAM](https://fedoraproject.org/wiki/Changes/SwapOnZRAM),
[systemd-zram-generator defaults](https://github.com/systemd/zram-generator/blob/main/zram-generator.conf.example),
HAOS) is 25–50%. We pick 50% (~500 MB on 1 GB) with **lz4**
compression. zstd has ~30% better compression ratio but
decompresses ~3× slower per page on Cortex-A76; for a
real-time audio device, predictable decompression latency
matters more than raw compression ratio.

**Layer 1c — vm.* sysctl tuning
(`/etc/sysctl.d/99-jts-vm.conf`).** Reclaim and watermark
tuning for low-RAM ARM with a zram-only swap topology. The
load-bearing knobs:

- `vm.swappiness=100` — bias the reclaim algorithm toward
  using compressed RAM over evicting hot file cache. **Note**:
  Fedora ships 180 on zram-only systems. We pick 100 as a
  conservative middle because audio jitter from zram
  decompression is the dominant risk on this box — we'd
  rather evict file cache (re-readable from SD) than swap anon
  pages (decompression latency hits the audio path).
- `vm.page-cluster=0` — universal recommendation for zram
  (no spatial locality on compressed pages, default 8-page
  read-ahead just wastes RAM).
- `vm.watermark_scale_factor=125` — wake kswapd at 1.25%
  headroom (default 0.1% is ~1 MB on a 991 MB box, kswapd
  burns through it in milliseconds under burst → reclaim
  becomes bursty).
- `vm.min_free_kbytes` — kernel's reserved memory floor.
  **Computed per-Pi at install time** by `install.sh`'s
  `migrate_memory_resilience` step: `clamp(0.02 × MemTotal_kB,
  8192, 262144)` = 2% of RAM with an 8 MB floor (matches Pi
  Foundation default; never reduce below) and a 256 MB cap (so
  a 16 GB Pi doesn't reserve unreasonably much). Resolves to
  20 MB on 1 GB Pi, 40 MB on 2 GB, 80 MB on 4 GB, 160 MB on
  8 GB. 2% is in the safe band per Linux Hint guidance
  (>5% causes OOM-immediate); matches Pop!_OS's runtime
  safeguard floor pattern at [pop-os/default-settings#163](https://github.com/pop-os/default-settings/pull/163).
  Doctor reads the installed conf and verifies live `/proc/sys/vm/min_free_kbytes`
  matches — drift surfaces immediately.

Full annotated config at [`deploy/sysctl/99-jts-vm.conf`](../deploy/sysctl/99-jts-vm.conf).

**Layer 1d — MGLRU thrashing prevention
(`/etc/tmpfiles.d/jts-mglru.conf`).** This is the single most
direct fix for the 2026-05-23 incident shape. MGLRU has been
on by default in the RPi kernel since 6.1. Setting
`min_ttl_ms=1000` tells the kernel: protect any page that was
accessed in the last 1 second from reclaim, even if it means
triggering OOM-kill instead. Empirically validated by pelwell
on Pi 4 Chromium ([forum thread](https://forums.raspberrypi.com/viewtopic.php?t=344246)).
**Watch for spurious kills** in week 1 after this ships;
reduce to 500 if anything legitimate is being killed under
normal load.

**Drift detection.** `jasper-doctor` adds five Stage-1 checks
that fail-soft (warn, not fail) so the operator sees
divergence at a glance:

- `check_oom_score_adj` — actual vs expected adj per critical daemon
- `check_zram_size_ratio` — WARN if zram > 60% of RAM
- `check_mglru_min_ttl` — WARN if min_ttl_ms drifted from 1000
- `check_sysctl_drift` — WARN on vm.* divergence from
  `/etc/sysctl.d/99-jts-vm.conf`
- `check_memory_headroom` — RAM-tier-aware percentage thresholds
  with absolute MB floors: WARN if `MemAvailable < max(100 MB,
  10% × RAM)`, FAIL if `< max(30 MB, 3% × RAM)`. Fires on every
  Pi SKU (1 GB through 16 GB) — on 8 GB Pi, warn fires at
  800 MB available, fail at 240 MB.

**What Stage 1 explicitly does NOT do.** It doesn't enable the
memory cgroup, so the `MemoryHigh=` / `MemoryMax=` directives
already present in six unit files remain silent no-ops. It
doesn't install a userspace OOM killer (earlyoom / systemd-oomd).
It doesn't carve daemons into slices. Those are Stage 2 +
Stage 3 — see the dedicated section below for what they'd
add, what they'd cost, and the explicit triggers that should
prompt picking the work back up.

**What Stage 1 expected outcome is.** Re-running the 2026-05-23
incident shape (PIO compile on 1 GB Pi): kernel OOM-killer
fires within ~20 s (vs >2 min before), picks the offending
process (lower OOMScoreAdjust than any jasper-* daemon), MGLRU
prevents the grind-through-zram-thrash phase, system recovers
without manual intervention. Verified by `jasper-doctor`
post-install:

```sh
sudo /opt/jasper/.venv/bin/jasper-doctor | grep -E "OOM|zram|MGLRU|vm.|memory"
```

### Stage 2 — cgroup memory + slice architecture (audio-protection subset shipped 2026-05-24)

**Status update (2026-05-24)**: the audio-protection subset of Stage 2
shipped after the 2026-05-24 stress test produced empirical evidence
that the trigger condition documented below ("audio xruns correlated
with memory pressure") was met. **The rest of Stage 2** — per-daemon
`MemoryHigh=`/`MemoryMax=` enforcement on non-audio slices,
systemd-oomd integration — remains deferred per the trigger analysis.

#### 2026-05-24 stress test (the evidence that triggered the audio subset)

`stress-ng --vm 1 --vm-bytes 300M --vm-keep --timeout 60s` on the 1 GB
Pi 5 with Stage 1 + T5.1 + T5.2 in place. The system *survived*:

- Load capped at 3.07 (no scheduler death spiral)
- SystemSupervisor probes stayed green (sshd / `/healthz` /
  `/proc/loadavg` all responded within budget)
- All 6 jasper-* daemons stayed active
- OOM-killer never had to fire — zram absorbed the pressure

**But the music played during the stress was audibly degraded** —
"splotchy, crushed" per the operator's real-time report. Forensics
captured immediately after the stress:

```
jasper-aec-bridge: VmLck=16 kB    VmSwap=43056 kB   ← 42 MB in zram
jasper-camilla:    VmLck=64 kB    VmSwap=416 kB
```

Mechanism: under memory pressure, the kernel evicted the audio-path
daemons' pages to zram. Subsequent audio-frame access triggered
zstd decompression (~10-15 µs per page on Cortex-A76), and the
decompression-latency variance exceeded the ALSA buffer's slack
window (~10 ms), causing per-frame underruns. `LimitMEMLOCK=infinity`
in the unit files grants permission to lock memory but the daemons
weren't actually calling `mlockall()` — only ~64 kB locked.

This is the failure mode `MemorySwapMax=0` on a cgroup explicitly
prevents — and it's the audio-protection subset of Stage 2 that
shipped in response. Same-day evidence → same-day fix.

#### What the audio-protection subset does

Two changes:

**1. Enable the memory cgroup controller.** `install.sh`'s
`migrate_cgroup_memory_enabled` idempotently removes an explicit
`cgroup_disable=memory` token from `/boot/firmware/cmdline.txt` if one
is present, then adds three tokens:
- `cgroup_enable=memory` — overrides the Raspberry Pi boot-time
  `cgroup_disable=memory` injection
- `cgroup_memory=1` — paired token (legacy, harmless on current kernels)
- `psi=1` — enables `/proc/pressure/` if `CONFIG_PSI=y` (no-op
  otherwise). Not required for Stage 2 audio but unlocks PSI
  observability for future Stage 3 work.

**Reboot required** — kernel only reads cmdline at boot. On Raspberry Pi
OS, `/proc/cmdline` may still show a DTB-injected
`cgroup_disable=memory` alongside the enable tokens; the load-bearing
verification is whether `/sys/fs/cgroup/cgroup.controllers` includes
`memory`.

**2. Carve audio + mic daemons into protected slices** with
`MemorySwapMax=0`:

```
jts-audio.slice          ← jasper-fanin, jasper-camilla, shairport-sync,
                            librespot, bluealsa-aplay
                         ← bonded grouping: jasper-snapclient /
                            jasper-snapserver managed units
                          MemorySwapMax=0
                          ManagedOOMPreference=avoid

jts-mic.slice            ← jasper-aec-bridge
                          MemorySwapMax=0
                          ManagedOOMPreference=avoid
```

`Slice=` directives in the unit files (or drop-ins for unit files
JTS doesn't own, like `bluealsa-aplay.service.d/jts-slice.conf`)
assign each daemon to its protected slice. Once `MemorySwapMax=0` is
in effect, the kernel literally cannot swap those daemons' pages to
zram — under memory pressure it either keeps the pages in real RAM,
sheds pages from other (unprotected) cgroups, or OOM-kills the
audio daemon (a clean restart that's preferable to silent jitter).

Why slices rather than `MemorySwapMax=0` on each unit directly:
expressiveness for future scaling. New audio daemons go into the
existing slice via one `Slice=` line; policy lives in one place.

Why audio + mic are separate slices: different failure-mode
semantics. Audio jitter = audible glitch; mic jitter = missed wake
events or stuttery voice turns. Future Stage 3 oomd policy might
differ between them.

#### Verification post-deploy + reboot

```sh
# 1. Memory cgroup actually online
cat /sys/fs/cgroup/cgroup.controllers | tr ' ' '\n' | grep memory

# 2. MemorySwapMax=0 in effect on the slice
systemctl show jts-audio.slice -p MemorySwapMax     # → MemorySwapMax=0
cat /sys/fs/cgroup/jts-audio.slice/memory.swap.max  # → 0

# 3. Each audio daemon is actually IN the slice
systemctl show jasper-camilla -p Slice              # → Slice=jts-audio.slice
# Bonded grouping member example (snapcast units run on any paired box):
systemctl show jasper-snapclient -p Slice           # → Slice=jts-audio.slice

# 4. Daemons aren't swapping (this is the load-bearing one)
for unit in jasper-camilla jasper-aec-bridge shairport-sync librespot bluealsa-aplay jasper-snapclient; do
    pid=$(systemctl show -p MainPID --value ${unit}.service)
    [ "$pid" != "0" ] && echo "$unit: $(grep VmSwap /proc/$pid/status)"
done
# All should show VmSwap: 0 kB (or very near zero)

# 5. Doctor verifies all of the above in one shot
sudo /opt/jasper/.venv/bin/jasper-doctor | grep -E "cgroup memory|audio path"
```

#### What the rest of Stage 2 would still add (still deferred)

The audio-protection subset doesn't enforce `MemoryHigh=`/`MemoryMax=`
on non-audio daemons. The 6 unit files that already have those
directives (mux, input, usbsink, system-web, bluetooth-web,
librespot) **do now enforce** as a side-effect of memory cgroup
being on — those values become live the moment the controller is
enabled.

But there are no per-daemon caps on `jasper-voice` (~150 MB) or
`jasper-control` (~35 MB) yet, and no per-slice cap on the
non-audio path. Adding those would catch:
- Slow memory leak in jasper-voice or jasper-control
- A future regression that ballooned a daemon's working set

These deferrals remain valid until the same "evidence → trigger"
discipline that shipped the audio subset shows the same need on
the non-audio path. The trigger conditions documented at the end
of this section still apply.

---

### Stage 2 — full architecture (still deferred, with explicit triggers)

Below this point is the original deferred analysis (predating the
2026-05-24 audio-subset ship). Kept for the rest of Stage 2 (per-
daemon caps, systemd-oomd) that hasn't yet been justified by
observed evidence.

#### What Stage 2 would do

Two changes, paired:

**1. Enable the Linux memory cgroup controller.** The Pi 5's device-
tree blob ships with `cgroup_disable=memory` in the kernel's boot
arguments (an RPi-Foundation choice to save ~32 bytes per page of
accounting overhead — ~8 MB on a 1 GB Pi). Adding
`cgroup_enable=memory` to `/boot/firmware/cmdline.txt` and rebooting
flips the controller on. Verified working on Pi 5 + kernel 6.12.x
across the K3s / Docker / Home Assistant Supervised communities; no
known stability regressions on JTS-relevant workloads.

**Once on, the `MemoryHigh=` / `MemoryMax=` directives that already
exist in 6 unit files** (`jasper-mux.service`, `jasper-input.service`,
`jasper-usbsink.service`, `jasper-system-web.service`,
`jasper-bluetooth-web.service`, `librespot.service`) **start
enforcing**. Today they're silent no-ops — systemd accepts them,
kernel ignores them because there's no memory cgroup. This is a
real bug class: the operator looks at the unit file and assumes
they have protection, but they don't.

**2. Carve daemons into purpose-named slices.** Express memory
policy declaratively instead of per-unit:

```
jts-audio.slice    ← jasper-fanin, jasper-camilla, shairport-sync, librespot, bluealsa
                     MemorySwapMax=0          # audio pages NEVER touch zram
                     ManagedOOMPreference=avoid

jts-mic.slice      ← jasper-aec-bridge
                     MemorySwapMax=0          # realtime mic, same logic

jts-control.slice  ← jasper-control, jasper-mux, jasper-input
                     MemoryHigh=120M MemoryMax=180M

jts-voice.slice    ← jasper-voice
                     MemoryHigh=220M MemoryMax=320M
                     ManagedOOMMemoryPressure=kill   # oomd kills this first

jts-wizard.slice   ← jasper-{web,system-web,bluetooth-web,correction-web,dial-web}
                     MemoryHigh=64M MemoryMax=128M
                     ManagedOOMMemoryPressure=kill   # cheap to kill, re-spawned
```

The slice abstraction means a new daemon is one `Slice=jts-X.slice`
drop-in away from inheriting the right policy. The `MemorySwapMax=0`
on audio + mic is the single most defensible addition: audio pages
never sitting in zram → no decompression-jitter window during memory
pressure → no xrun-class failure mode from that path.

#### What new protection it adds (vs what's already shipped)

Three failure classes Stage 1 + T5.x do NOT catch:

1. **Slow memory leak in a single daemon** (e.g. a regression in
   jasper-voice). Stage 1's OOMScoreAdjust biases the kernel
   OOM killer away from jasper-* but doesn't cap per-daemon
   growth — a leak gradually starves everything else. With
   `MemoryMax=320M` on `jts-voice.slice`, the cgroup OOM-killer
   fires on jasper-voice specifically, `Restart=on-failure`
   brings it back fresh, the rest of the system is unharmed.
2. **Audio jitter from zram decompression.** Under memory
   pressure, jasper-camilla's pages can be swapped to zram.
   Next audio frame triggers a page-fault → lz4 decompression
   (~5-15 µs on Cortex-A76, more on zstd). With a small ALSA
   buffer (~10 ms) one bad timing window = an xrun.
   `MemorySwapMax=0` on `jts-audio.slice` says these pages
   never go to zram → eliminates the class.
3. **`systemd-oomd` for surgical slice-level kills.** Once
   cgroup memory + PSI are enabled, oomd can read pressure
   signals and kill a whole slice (e.g., `jts-wizard.slice`)
   before the kernel OOM-killer fires. More targeted than the
   kernel's badness heuristic.

#### What Stage 2 would cost

| Cost | Estimate |
|---|---|
| Kernel-side RAM (memory cgroup accounting) | ~8 MB on a 1 GB Pi (~0.8%) |
| Userspace RAM (systemd-oomd if shipped as part of Stage 3) | ~15 MB |
| Engineering time | ~2-3 hours: cmdline guardian + 5 slice unit files + 6 Slice= drop-ins + 4 new doctor checks + tests |
| Reboot | One, after the cmdline.txt edit |
| Risk: existing `MemoryHigh/Max` values become effective | Moderate. They were sized when they were no-ops, so we don't actually know if `MemoryMax=120M` on jasper-mux is right or generous. First post-deploy soak may surface a daemon that briefly exceeds during startup. Mitigation: ship with generous headroom + monitor `/sys/fs/cgroup/.../memory.events` for a week before tightening |

#### Why it's deferred (the honest framing)

- **The motivating incident is covered.** Stage 1 (prevention) +
  T5.2 (recovery) caught the 2026-05-23 shape end-to-end. Shipping
  Stage 2 to catch hypothetical-future leaks is premature without
  evidence.
- **The existing `MemoryHigh/Max` values are unvalidated.** They've
  never enforced — we don't actually know they're correctly sized.
  Enabling them blind risks restarts on healthy daemons we'd then
  need to debug. The right pattern is *measure first*: run Stage 1
  + telemetry for ≥30 days, see what daemons' actual memory
  profiles look like, then size the caps.
- **The audio-xrun failure mode is theoretical here.** If we'd
  seen the symptom (music glitching during memory pressure), the
  `MemorySwapMax=0` fix would be obvious. We haven't observed
  it on jts2.local. Worth instrumenting first (xrun count over
  time correlated with PSI pressure events) before shipping the
  structural fix.
- **systemd-oomd has known issues** on Pi-class hardware: the
  cgroup memory enablement quirks documented in
  [HANDOFF-tier5-watchdog-liveness.md](HANDOFF-tier5-watchdog-liveness.md)
  Option E, and the well-documented "kills the whole cgroup with no
  per-process forensics" complaint from Fedora's 34-era rollout.

#### Triggers for shipping Stage 2

Any one of:
- **Observed slow memory leak.** MemAvailable trends down over days
  in `/system/`'s memory sparkline (60-min ring buffer in
  `system_metrics.py` makes this easy to spot).
- **Audio xruns correlated with memory pressure.** `/proc/asound/card*/pcm*/sub*/xrun`
  counter ticking up during PSI memory-pressure events.
- **A new dependency that might leak.** Adding a Python lib or
  model that's known-leaky and we want a hard cap to bound it.
- **Open-source adoption stress.** If JTS sees significant fork
  activity and other operators report leak shapes we don't see
  on the original hardware.

Until then, the engineering hours are better spent elsewhere. The
HANDOFF entry exists so the next contributor doesn't have to
re-derive what the right structural fix is — only whether the
trigger is present yet.

### Hardware-event recovery — sidebar to the ladder

Separate from the watchdog ladder above, one failure class is worth
calling out because the ladder doesn't catch it: daemons that **exit
cleanly at startup** because a USB device is absent. This is not a
hang and not a sibling-daemon issue — it's the dependency physically
missing at the moment the daemon tries to open it.

The 2026-05-11 sequence:

1. Power-cycle the Pi while nothing is plugged into the Apple dongle's
   3.5 mm jack. The dongle drops its USB Audio Class interfaces
   without an analog load, so the Pi boots with the dongle
   USB-enumerated but no Card A.
2. `jasper-camilla`, `jasper-aec-bridge`, and `jasper-voice` all try
   to open `hw:CARD=A`, get `ValueError: No output device matching
   'jasper_out'`, and exit with code 1.
3. systemd retries each per `Restart=on-failure`, hits
   `StartLimitBurst` after ~5 attempts, parks the units as failed,
   stops watching.
4. User plugs the speaker back into the 3.5 mm jack. Card A appears
   in `/proc/asound/cards`. Nothing is monitoring for this; the units
   stay parked and the speaker stays silent until manual
   `systemctl reset-failed && start`.

`WatchdogSec` doesn't help — the daemons exited cleanly, they didn't
hang. `Restart=on-watchdog` only catches the watchdog timeout, not
arbitrary exits. Bumping `StartLimitBurst` higher would just delay
the same parked-failed outcome.

Fix, part one: a udev rule on the dongle's USB IDs that triggers
`jasper-dongle-recover.service`
(`deploy/systemd/jasper-dongle-recover.service`) when Card A appears,
which runs `systemctl reset-failed`, starts the output graph
(`jasper-camilla`, `jasper-outputd`, and
`jasper-audio-hardware-reconcile.service`), and then best-effort starts
`jasper-aec-reconcile.service` when that mic/voice policy unit is
installed. Idempotent — when the daemons are already healthy it's a no-op.
This shape is streambox-safe: Zero-class streamboxes have output/DSP but no
AEC brain service. The rule
(`deploy/udev/99-jasper-apple-dongle.rules`) uses `SYSTEMD_WANTS`
rather than `RUN+=` so systemctl dispatches via PID 1 asynchronously
and udev's event pipeline stays responsive.

Fix, part two: `jasper-aec-reconcile` owns the mic/AEC policy that
the old install-time `enable_aec_if_compatible` could not express.
The stale-state bug was: an earlier healthy boot could set
`JASPER_MIC_DEVICE=udp:9876`, then a later boot without the XVF
Array would make `jasper-aec-bridge` fail because `/proc/asound/Array`
was gone while `jasper-voice` still listened on UDP for packets that
would never arrive. The reconciler closes that loop:

- `JASPER_AEC_MODE=auto` + 6-channel `JASPER_AEC_MIC_DEVICE` present:
  set `JASPER_MIC_DEVICE=udp:<port>`, enable/start
  `jasper-aec-init` + `jasper-aec-bridge`, restart voice.
- A configured direct mic candidate is present but AEC is unavailable
  (2-channel firmware or AEC disabled): set `JASPER_MIC_DEVICE` to
  that candidate, keep the bridge off, restart voice.
- No candidate mic is present and the current value is one JTS owns
  (`Array`, `udp:<port>`, or legacy `hw:N,1`): clear stale UDP back to
  the first candidate and stop voice so it does not watchdog-loop.
- A genuinely custom `JASPER_MIC_DEVICE` is left untouched. This is the
  escape hatch for future mics while we keep the production default
  simple.

The future-mic hook is intentionally small: `JASPER_AEC_MIC_DEVICE`
defaults to `Array`, and `JASPER_MIC_DEVICE_CANDIDATES` defaults to
`Array`. If we add another supported mic later, add it to the
candidate list (comma-separated, or shell-quoted if using spaces) and
the same reconciler can select it as the direct fallback. If the future
mic needs its own AEC path, that should be a deliberate second policy
branch rather than baking more assumptions into the Array path.

### WiFi profile recovery — sidebar to the ladder

Same shape as the dongle-recover case above, with the missing
dependency being a **file on the local filesystem** instead of a
USB device. Lives in this sidebar rather than as a new tier because
it's declarative reconciliation of state-vs-config drift, not
liveness recovery.

The 2026-05-23 sequence:

1. USB-C power yanked during a power-splitter swap. The Pi's root
   ext4 partition had an in-flight write to
   `/etc/NetworkManager/system-connections/<SSID>.nmconnection`.
2. On reboot, ext4 journal recovery on the dirty mount discarded
   the partially-written file entirely.
3. The Pi came up with NO WiFi profile at all. NetworkManager
   probed for known networks, found none, and stayed in a
   disconnected state.
4. Speaker unreachable on the LAN. Recovery required HDMI +
   USB-keyboard console (~1 hour) to type `nmcli connect ssid
   password ...` by hand.

The behavioural fix — graceful shutdown via the `/system/` Power
Off button — is being adopted separately. The WiFi profile
guardian is the software floor under it: even with graceful
shutdown adopted, filesystem corruption / accidental `rm` /
botched migrations can still erase the keyfile, and the Pi
should self-heal rather than brick.

Fix shape mirrors `jasper-aec-reconcile` exactly:

- **Wizard-owned stash** at `/var/lib/jasper/wifi_guardian.env`
  (mode 0600, env-var format with `JASPER_WIFI_SSID` /
  `JASPER_WIFI_PSK` / `JASPER_WIFI_KEY_MGMT`).
- **Pure-bash policy script** at
  `/usr/local/sbin/jasper-wifi-guardian` (from
  `deploy/bin/jasper-wifi-guardian`), driven by
  `jasper-wifi-guardian.service` (`Type=oneshot`, after
  `NetworkManager-wait-online`, gated by `ConditionPathExists=`
  on the stash).
- **Write hooks** in the `/wifi/` wizard
  ([`jasper/web/wifi_setup.py`](../jasper/web/wifi_setup.py)) —
  `connect_new` writes from the PSK on the wire, `connect_saved`
  reads via `nmcli -s`, `forget` clears when the SSID matches.
- **Install-time seed** in `install.sh`'s `migrate_wifi_guardian`
  so SSH-driven setup paths arm recovery on the first deploy
  rather than waiting for the user to open the wizard.

What the script does at boot:

- Active WiFi SSID matches stash → no-op (`steady_state`).
- Active WiFi SSID differs from stash → no-op (`stash_stale`,
  operator switched networks via SSH; we don't know which is
  "right", don't stomp the working network).
- No active WiFi, but a profile for the stashed SSID exists →
  `nmcli connection up SSID` (`activate`).
- No active WiFi and no profile → THE INCIDENT CASE.
  `nmcli dev wifi connect SSID password $PSK` (`recreate_attempt`).
  On failure, delete the broken half-profile and exit non-zero so
  the operator notices.

Why a custom guardian rather than NM-native restoration? There
isn't one. NetworkManager has no documented "restore profile from
backup" path; the standard pattern people roll themselves is a
dispatcher script on `up` events plus a sidecar config store —
which is exactly the shape of this guardian.

PSK redaction is enforced in three layers:
- **Bash script:** never includes `$PSK` in `emit` / `log` calls;
  scrubs literal-PSK and `password \S+` patterns from nmcli
  stderr before re-emitting on `recreate_fail`.
- **Python wizard hooks:** logs only SSID + `key_mgmt`; the PSK
  travels through `_run_nmcli_secret`'s existing scrubber.
- **`/state` snapshot + doctor:** read the stash for SSID but
  never include the PSK in any output. `/state` is
  unauthenticated on the LAN; doctor output ends up in install
  transcripts and bug reports.

Diagnostic surfaces:

```sh
# Per-event structured lines (one per guardian run):
journalctl -u jasper-wifi-guardian | grep event=wifi_guardian

# Live state from jasper-control:
curl -s http://jts.local:8780/state | jq .resilience.wifi_guardian

# Doctor (warns on stash absence + drift; informational only):
sudo /opt/jasper/.venv/bin/jasper-doctor | grep "WiFi profile guardian"

# Manual trigger (after a known-bad boot to retry now):
sudo /usr/local/sbin/jasper-wifi-guardian --reason manual
```

Out of scope (deferred, see PR #266 description):
- **NM dispatcher script** on `up` events. Adds complexity for an
  asymmetry the wizard hooks already cover.
- **Multi-network stash.** The speaker doesn't travel.
- **WPA-Enterprise.** Wizard rejects it at write-time; script
  defends in depth (`event=wifi_guardian.skip reason=enterprise`).

---

## Implementation map

For anyone touching the resilience code:

- `jasper/watchdog.py` — the `Heartbeat` class. Sentinel pattern,
  graceful no-op when `NOTIFY_SOCKET` is unset (lets the daemons
  run under `python -m` for development without breaking).
- `jasper/cli/aec_bridge.py` — `_aec_loop` calls `heartbeat.bump()`
  after each successful processed frame; UDP socket replaces the
  old `sd.RawOutputStream`. Also keeps `BridgeStalled` as a
  belt-and-suspenders explicit mic-empty detector — catches the
  specific PortAudio-dead case faster than `WatchdogSec` and with
  a clearer log line.
- `jasper/voice_daemon.py` — `WakeLoop` accepts `heartbeat:
  Heartbeat | None` and bumps it at the top of each mic-frame
  iteration in `run()`. The mic source is constructed via
  `make_mic_capture(cfg.mic_device, ...)` which dispatches to
  `UdpMicCapture` for `udp:PORT` device strings.
- `jasper/audio_io.py` — `UdpMicCapture`, `parse_udp_device`,
  `make_mic_capture` factory. Queue init is deferred to
  `__aenter__` so the classes are construct-safe from sync code.
- `deploy/systemd/jasper-{aec-bridge,voice}.service` — the
  `Type=notify` + `WatchdogSec=30s` + `Restart=on-watchdog` +
  `TimeoutStopSec=5s` block.
- `deploy/modprobe.d/snd-aloop.conf` — single-card config
  (`enable=1 index=6 id=Loopback pcm_substreams=8`); the
  historical-note comment block explains the retirement of
  `LoopbackAEC`.
- `deploy/bin/jasper-aec-reconcile` — the mic/AEC policy reconciler.
  It reads `/etc/jasper/jasper.env` plus
  `/var/lib/jasper/aec_mode.env`, detects the configured mic card under
  `/proc/asound`, clears stale UDP when the Array is absent, and starts
  or parks `jasper-aec-*` + `jasper-voice` accordingly.
- `deploy/systemd/jasper-aec-reconcile.service` — oneshot wrapper used
  at install, boot, and udev-triggered hardware changes.
- `deploy/install.sh:reconcile_aec_state` — seeds
  `/var/lib/jasper/aec_mode.env` with `JASPER_AEC_MODE=auto`, enables
  the reconciler unit, and runs it once at install time.
- `deploy/udev/99-jasper-apple-dongle.rules` — three rules keyed
  on the dongle's USB IDs: Headphone-100% pin on hotplug, USB
  autosuspend off, and `SYSTEMD_WANTS` trigger for the recovery
  service on Card A appearance.
- `deploy/udev/99-jasper-aec-reconcile.rules` — generic ALSA
  `controlC*` add/remove trigger for the reconciler. The service itself
  is what stays conservative about which mic config it owns.
- `deploy/bin/jasper-audio-hardware-reconcile` +
  `deploy/systemd/jasper-audio-hardware-reconcile.service` +
  `deploy/udev/99-jasper-audio-hardware-reconcile.rules` — the same
  event-driven shape for output DAC roles. The oneshot classifies the
  selected final-output DAC, updates JTS-owned DAC identity/asound
  state for recognized roles, writes
  `/run/jasper-output-hardware/output_hardware.json` with observed-vs-active hardware
  facts, applies the explicit DAC8x-only `JASPER_OUTPUT_DAC_ROUTE`
  render path when configured, and enables Apple mixer helpers only for
  the Apple output role. A recognized role must render the managed ALSA
  template before the reconciler publishes new active env values; if
  rendering fails, the previous runtime env remains in place. If no
  recognized output DAC is present, it parks `jasper-voice` and
  `jasper-outputd` instead of leaving stale direct-DAC state active;
  recognized DAC arrival restarts outputd when state changed and
  reset-failed+starts outputd when the arrival is value-neutral, so a
  condition-parked final-output owner recovers without a full deploy.
- `deploy/systemd/jasper-dongle-recover.service` — `Type=oneshot`
  unit that `reset-failed`s the audio daemons, restarts the output graph
  (`jasper-camilla`, `jasper-outputd`, `jasper-audio-hardware-reconcile`),
  and then best-effort starts `jasper-aec-reconcile` on full speakers so
  mic/AEC/voice state matches present hardware. Triggered by the udev rule
  above; idempotent so re-fires on rapid replug are harmless and
  streambox installs without AEC do not fail the recovery.
- `deploy/journald/50-jts-persistent-storage.conf` — Tier 5
  forensics pairing. Installed by `install.sh`'s
  `install_journald_persistent_storage()` to
  `/etc/systemd/journald.conf.d/`. Overrides RPi OS's
  `40-rpi-volatile-storage.conf` so journal entries from the boot
  preceding a watchdog reset survive to `/var/log/journal/`.
- `jasper/control/shairport_supervisor.py` — Tier 3 supervisor for
  shairport-sync's AP2 control plane. `ShairportSupervisor.run()` is
  the supervisor loop; `_tick()` is the pure policy under test.
  Overridable `probe`, `is_session_active`, `restart_shairport` IO
  methods are the seams for unit testing. Module-level `snapshot()`
  feeds `/state.resilience.shairport`. Started from `server.py:main`
  via `start_supervisor()`; no-op when
  `JASPER_SHAIRPORT_SUPERVISOR=disabled`.
- `tests/test_watchdog.py` — sentinel-contract tests
  (fresh/stale/recovery/disabled-fallback).
- `tests/test_udp_mic_capture.py` — UDP receiver contract
  (parse-forms, factory dispatch, end-to-end frame yield).
- `tests/test_aec_reconcile.py` — stale-UDP and hardware-mode tests
  for the reconciler.
- `tests/test_shairport_supervisor.py` — policy contract
  (threshold / gate / rate-limit / failure-mode degradation) + default
  RTSP probe IO contract against a real asyncio TCP server.

---

## Verification

After deploying the resilience changes, the following should be
true on the running Pi. Useful for `jasper-doctor` follow-ups or
manual verification.

- `aplay -l` lists `Loopback` (card 6) and no `LoopbackAEC`.
- `systemctl show jasper-aec-bridge -p Type -p WatchdogUSec -p
  Restart -p TimeoutStopUSec` returns `Type=notify
  Restart=on-watchdog TimeoutStopUSec=5s WatchdogUSec=30s`. Same
  for `jasper-voice`.
- `journalctl -u jasper-aec-bridge | grep "udp output"` shows
  `udp output: dest=127.0.0.1:9876 frame=1280 samples (2560 bytes)`.
- `journalctl -u jasper-voice | grep "UdpMicCapture"` shows
  `UdpMicCapture listening on 127.0.0.1:9876 (frame=1280 samples
  @ 16000 Hz)`.
- `ss -ulpn | grep 9876` shows the voice process owning the UDP
  socket.
- If the Array is absent, `journalctl -u jasper-aec-reconcile -e`
  should show stale UDP being cleared to `Array`; `jasper-aec-bridge`
  should be disabled/inactive and `jasper-voice` should be stopped
  rather than watchdog-looping.
- `journalctl -u jasper-control | grep 'event=shairport.start'` shows
  one supervisor-start line per jasper-control boot. Once the cold
  start has elapsed (60 s default), `curl -s localhost:8780/state |
  jq .resilience.shairport` returns `enabled=true` with a recent
  `last_probe_at` and `last_probe_ok=true`.
- Tier 5 hardware watchdog active: `systemctl show -p
  RuntimeWatchdogUSec` returns `1min` (RPi OS default) and
  `WatchdogDevice=/dev/watchdog0`.
- Persistent journal active: `sudo journalctl --header | grep
  "File path"` shows `/var/log/journal/...` (not
  `/run/log/journal/...`), and `sudo journalctl --list-boots`
  enumerates more than one boot once the Pi has rebooted at least
  once since PR #160 landed.

Smoke test that the watchdog actually catches a wedge:

```sh
sudo kill -STOP $(pgrep -f jasper-aec-bridge | head -1)
sudo journalctl -fu jasper-aec-bridge
# within 30 s: "Watchdog timeout (limit 30s)!" → SIGABRT → restart
```

Smoke test that the dongle-recovery udev rule fires:

```sh
# Pull the speaker cable from the dongle's 3.5 mm jack, wait until
# `cat /proc/asound/cards` no longer lists Card A, then re-seat it.
sudo journalctl -fu jasper-dongle-recover
# within 1-2 s of re-seat: ExecStart= lines, then exits successfully.
# `systemctl is-active jasper-camilla jasper-voice` should both
# return `active` after, regardless of prior state.
```

---

## What we explicitly did NOT do, and why

- **PipeWire migration.** Out of scope per project policy. The
  resilience win comes from removing snd-aloop from the
  bridge↔voice path entirely, not from replacing the userspace
  audio stack. (Note: the exclusion was scoped to this resilience
  question. [HANDOFF-barge-in.md](HANDOFF-barge-in.md) re-opens
  it honestly as a costed Option B when robust barge-in is the
  motivation — different question, different trade.)
- **In-process AEC** (embed `jasper_aec3` directly in
  `jasper-voice`). Simpler but expands voice's blast radius — a
  crash in AEC3 today takes down only the bridge; in-process it
  would take down wake-word too. UDP preserves the isolation.
- **A separate watchdog daemon** (monit, supervisord, custom).
  systemd's built-in `sd_notify` + `Restart=on-watchdog` does
  everything those would, with no new process.
- **Tier 4 today.** Kernel-state recovery (`rmmod + modprobe
  snd_aloop`) waits on evidence of need. Tiers 1+2 ship in the
  AEC + voice paths; Tier 3 shipped after we observed the failure
  class it covers (shairport AP2 wedge, 2026-05-19); Tier 5 was
  always on (RPi OS default) and only needed the persistent
  journal pairing to become useful, which shipped in PR #160
  after the 2026-05-20 wedge investigation.
- **A generic "third-party daemon supervisor" framework.** Tier 3's
  shape (probe → gate → rate-limited restart) is reusable, but
  shairport is the only third-party daemon that has demonstrated the
  failure class. Lifting it into a generic supervisor before there's
  a second instance buys complexity, not value.

---

## References

- [Crash-Only Software, Candea & Fox (HotOS-IX 2003)](https://www.usenix.org/legacy/events/hotos03/tech/full_papers/candea/candea.pdf) — the conceptual frame.
- [Lennart Poettering, "systemd for Administrators, Part XV: Watchdogs"](http://0pointer.de/blog/projects/watchdog.html) — sd_notify + WatchdogSec design.
- [sd_notify(3) man page](https://www.freedesktop.org/software/systemd/man/latest/sd_notify.html).
- [sound/drivers/aloop.c on torvalds/linux](https://github.com/torvalds/linux/blob/master/sound/drivers/aloop.c) — the `loopback_cable` state machine that wedges on SIGKILL.
- [ALSA C library — PCM Interface (snd_pcm_recover, states)](https://www.alsa-project.org/alsa-doc/alsa-lib/group___p_c_m.html) — why `DISCONNECTED` is unrecoverable.
- PRs that shipped this design: [JTS#77](https://github.com/jaspercurry/JTS/pull/77) (Tier 1+2 watchdog), [JTS#93](https://github.com/jaspercurry/JTS/pull/93) (UDP transport + LoopbackAEC retirement).

---

Last verified: 2026-06-15
