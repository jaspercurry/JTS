# Handoff: resilience — the recovery ladder and what it catches

The speaker runs unattended in a home for years and must recover from failure
without a human. This doc is the map of the machinery that makes that true and
of what is deliberately not built.

Neighbours: [HANDOFF-hotplug-resilience.md](HANDOFF-hotplug-resilience.md) owns
the convergence contract for hardware attached/detached while running ·
[HANDOFF-tier5-watchdog-liveness.md](HANDOFF-tier5-watchdog-liveness.md) owns
the still-deferred T5.3–T5.5 option matrix ·
[HANDOFF-privilege-separation.md](HANDOFF-privilege-separation.md) owns the
polkit boundary the supervisors' restarts cross ·
[historical/resilience-incidents-2026-05.md](historical/resilience-incidents-2026-05.md)
holds the incident narratives and the never-shipped Stage 2 analysis.

Decisions extracted from this subsystem:
[ADR-0102](adr/0102-mic-transport-is-udp-localhost.md) (UDP mic transport),
[ADR-0103](adr/0103-config-apply-restarts-clear-the-flap-counter.md)
(`reset-failed` erases `NRestarts`),
[ADR-0104](adr/0104-per-daemon-memory-caps-stay-deferred.md) (Stage 2 stays
deferred).

## The ladder

Scoped to **liveness** failures — stuck processes, wedged supervisors, hung
subsystems. Memory-pressure prevention sits parallel to it; T5.1/T5.2 sit below
Tier 5, catching the "userspace dead but PID 1 alive" shape the hardware
watchdog structurally misses.

| Tier | Mechanism | Catches | Wired? |
|---|---|---|---|
| 1 | `sdnotify` heartbeat thread with progress sentinel | Logic deadlock, blocked event loop, slow loop. `bump()` is called from each successful frame; the heartbeat thread only pats systemd if `now - last_progress < 5 s`, so a wedged loop stops patting even though the thread keeps running. | ✅ `jasper/watchdog.py`, wired into bridge `_aec_loop` and voice `WakeLoop.run` |
| 2 | systemd `Type=notify` + `WatchdogSec=30s` + `Restart=on-watchdog` + `TimeoutStopSec=5s` + `StartLimitBurst=20` | Process exit, hang, fatal ALSA error. Fires 30 s after Tier 1 stops patting; fresh process in ~2 s. | ✅ `jasper-aec-bridge.service`, `jasper-voice.service` |
| 3 | Protocol-level liveness probe in `jasper-control` + conditional `systemctl restart`, gated on no active session and rate-limited | Third-party daemons that wedge at the protocol layer while passing systemd's liveness check. shairport-sync AP2 today. | ✅ `jasper/control/shairport_supervisor.py`, started from `server.py:main` |
| 4 | Kernel-state recovery (`rmmod && modprobe snd_aloop` after stopping consumers) | snd-aloop / dsnoop kernel-side wedges. | ❌ deferred. Its original motivation (bridge↔voice snd-aloop) is gone per ADR-0102; the music-chain Loopback has not shown this failure. Trigger: it wedges again. |
| 5 | BCM2712 hardware watchdog (`/dev/watchdog0`) patted by PID 1, plus persistent journald for post-mortem forensics | Kernel panic, PID 1 hang, total userspace wedge. | ✅ RPi OS Trixie ships `RuntimeWatchdogSec=1m`/`RebootWatchdogSec=2m`; JTS contributes `deploy/journald/50-jts-persistent-storage.conf` so logs survive the reset |

Tier 5 is the floor, not the first line: Tiers 1–4 catch in-process hangs and
protocol wedges faster (~30 s vs ~60 s) and with a smaller blast radius (one
daemon restart vs a full reboot).

Production observability lives under `/state.resilience`: `shairport`,
`grouping_supervisor`, `system_supervisor`, `wifi_guardian`, `bootloop_guard`,
`content_lane`, `identity`, `disk`, `multiroom_cascade`, `active_speaker_parked`.
The first three are resident supervisor snapshots, read by doctor's
`supervisor runtime snapshots` check so a supervisor that is kicking,
rate-limited, or failing to converge is visible in one-shot diagnostics.
`multiroom_cascade` is a bounded after-the-fact ring sourced from persistent
journal `event=multiroom.reconcile.*`, `event=restart_broker.*`, and
`event=grouping_supervisor.*` lines — deliberately small, fail-soft, and
fixed-shape, enough to reconstruct "what kicked what recently" without turning
`/state` into a log bundle; each entry carries the journal occurrence time
(`occurred_at`) and the sampler time (`observed_at`) separately.

## Tier 3 — third-party protocol supervision

Every 30 s ± 3 s jitter the shairport supervisor opens TCP to `127.0.0.1:7000`,
sends a minimal RFC 2326 `OPTIONS *`, and expects `RTSP/1.0 200` within 3 s.
After 3 consecutive failures, gated on MPRIS `PlaybackStatus != "Playing"`, it
issues `systemctl reset-failed + --no-block restart` on `shairport-sync.service`
and `nqptp.service` — the same units `scripts/airplay-reset.sh` touches.
Because `jasper-control` runs as a non-root user, that is **polkit-authorized**
against the `MANAGED_UNITS` allowlist (`deploy/polkit/49-jasper-control.rules`).

The constraints that make it safe:

- **The no-active-session gate is the load-bearing safety net.** A probe failure
  during real listening is more likely a hiccup than a wedge.
- **A deliberate disable is honored.** When AirPlay is off at `/sources/`,
  failing probes idle the supervisor
  (`event=shairport.probe_idle reason=unit_disabled`, surfaced as
  `resilience.shairport.unit_disabled`) instead of counting toward a restart —
  without it the supervisor revived a disabled unit ~90 s after the toggle. The
  related bypass, where MPRIS-unknown *and* systemd-inactive stands the session
  gate aside so a crashed unit can recover
  (`event=shairport.gate_bypass reason=unit_inactive`), could not by itself tell
  "crashed" from "turned off". The enablement check runs only on failing probes,
  so the healthy path stays subprocess-free; errors reading enablement fail
  toward supervising, never toward silently parking Tier 3.
- **Rate limit:** one supervisor-driven restart per 10 minutes. Past that the
  issue is upstream and a restart is the wrong hammer.
- **Failure modes degrade safely.** A probe exception counts as a probe failure
  (the wedge signature is "no response"); a gate exception fails safe to
  "active".
- **Off switch:** `JASPER_SHAIRPORT_SUPERVISOR=disabled`, exact match,
  case-insensitive; other values log a warning and proceed as `auto`.

Not designed to handle: MPRIS-says-Playing-but-RTSP-wedged (the user gets
silence; the fix is `/system/restart/audio`), or an nqptp wedge independent of
shairport (the restart bundles both units, the detector probes only shairport).
The probe → gate → rate-limited restart shape generalizes to `librespot` and
`bluez-alsa`; neither has demonstrated the failure class. **Do not preemptively
spread it.**

## T5.1 and T5.2 — the userspace-liveness floor

**T5.1: `StartLimitAction=reboot`** on the critical jasper-* units (outputd,
fanin, aec-bridge, voice, control). When one exceeds its `StartLimitBurst=`
within `StartLimitIntervalSec=`, systemd cleanly reboots the box — `reboot`, not
`reboot-force`, because a clean shutdown is essential on a 1 GB Pi to flush zram
dirty pages. Per-unit thresholds preserve transient tolerance for audio-device
dropouts (voice keeps 20/300; aec-bridge and control use 4/300). `jasper-voice`'s
first-time unconfigured-provider exit is excluded via `SuccessExitStatus=78` +
`RestartPreventExitStatus=78`; actual crashes still flow through T5.1.

**The budget is for CRASH loops, so a deliberate config-apply must not spend
it**: a reconciler restarting one of these daemons runs `systemctl reset-failed`
first. A daemon's own `Restart=` path never runs that reset, so genuine crash
loops still escalate. That call also erases `NRestarts`, a live flapping signal
— an accepted cost, not a bug: [ADR-0103](adr/0103-config-apply-restarts-clear-the-flap-counter.md).

**Camilla exception.** `jasper-camilla.service` uses `Restart=always` +
`StartLimitBurst=5/60`, but start-limit exhaustion runs
`OnFailure=jasper-camilla-recover.service` with `StartLimitAction=none` instead
of a raw reboot: the observed failure was camilladsp exiting cleanly with ALSA
`Device or resource busy` while deploy/renderers churned, and a reboot destroyed
the `/dev/snd` holder evidence and made a reachable Pi look killed.
`deploy/bin/jasper-camilla-recover` captures `fuser`/`lsof` and
`/proc/asound/*/status`, parks likely graph owners, tries one bounded
fanin→Camilla→outputd restart, and kicks the AEC/grouping reconcilers. If the
graph still cannot converge it **parks the core graph once**
([ADR-0175](adr/0175-a-failed-camilla-recovery-parks-the-core-graph-once.md)):
a `/run/jasper-camilla-recover.state` record (`reason`/`action`/`re_arm`, the
ADR-0141 vocabulary) plus `reset-failed` + `stop` on CamillaDSP, so `OnFailure=`
cannot re-enter and stop all eleven units again every cooldown. Surfaced by
`/state.resilience.camilla_recover` and doctor's `check_camilla_recover_park`;
retired by CamillaDSP starting again (`jasper-camilla.service`'s
`ExecStartPost` removes the record) — an operator start, a deploy, or a reboot. Doctor's `check_installed_settings_drift`
surfaces drift if a distro update removes either the reboot directives or
Camilla's handler.

**T5.1 circuit breaker.** `StartLimitAction=reboot` alone is unbounded across
boots — a *permanent* daemon failure would reboot the Pi every few minutes
forever. `jasper-bootloop-guard.service` (pure-bash oneshot, ordered `Before=`
the escalating units) persists boot timestamps to
`/var/lib/jasper/bootloop_guard_boots`; on the 3rd boot inside a 3600 s window it
writes **runtime** drop-ins (`/run/systemd/system/<unit>.d/`,
`StartLimitAction=none`). That changes only the escalation, not the rate limit:
the sick unit exhausts its burst and systemd parks it failed — visible in
`systemctl`/doctor — while the Pi stays reachable. Recovery is fix the cause,
then `systemctl reset-failed <unit> && systemctl start <unit>`. Drop-ins live in
`/run`, so a healthy boot self-re-arms with no operator action. Guarded units are
discovered by grepping `StartLimitAction=reboot`, deliberately excluding Camilla.
Fail-open on every error path; `event=bootloop_guard.ok|tripped|error` +
`/state.resilience.bootloop_guard`.

**Recoverable front-door and renderers.** nginx, the socket-activated web
daemons, source renderers, the Bluetooth agent, and `jasper-mux` use a generous
finite window (`StartLimitIntervalSec=600`, `StartLimitBurst=20`; package-owned
services get JTS drop-ins). Intentionally not "restart forever": transient OOM
or update pressure should not park safe services, but genuine config/code loops
still stop loudly.

**T5.2: `SystemSupervisor`** (`jasper/control/system_supervisor.py`), mirroring
the Tier 3 shape. Probes three layers every 30 s ± jitter: **sshd banner
exchange** on `127.0.0.1:22` (TCP accept plus banner read within 2 s — the
incident shape was sshd accepting the connect but never writing the banner);
**jasper-control's own `/healthz`** on `127.0.0.1:8780` (yes, it probes itself;
this catches "asyncio loop wedged but systemd thinks we're alive", and a
`429 Too Many Requests` from the bounded request-admission gate counts as
alive-but-shedding, because treating overload shedding as a failed liveness
probe would let a LAN request burst manufacture a reboot); and a
**`/proc/loadavg` read** within 1 s (kernel I/O stall).

After 3 consecutive failures on any probe, rate-limited to 1 reboot per 24 h, it
calls `systemctl --no-block reboot` (polkit-authorized). The rate-limit window is
enforced against a **wall-clock** last-reboot timestamp persisted to
`/var/lib/jasper/system_supervisor_reboot.json`, so it survives the reboot it
just issued — otherwise a permanent userspace wedge would reboot-loop every
cold-start window forever and the household could never reach the box. Off via
`JASPER_SYSTEM_SUPERVISOR=disabled`. Doctor surfaces the persisted state file
(`supervisor reboot state`): missing → ok, corrupt or future-dated beyond NTP
skew → warn, since both are silent fail-open at runtime.

### Memory-pressure resilience (Stage 1)

Ships the layer that works on the stock RPi 5 kernel without the memory cgroup
controller, which the Pi 5 DTB disables
([raspberrypi/linux#5933](https://github.com/raspberrypi/linux/issues/5933)).

**1a — `OOMScoreAdjust` ladder.** `jasper/_oom_adj.py` is the single source of
truth, shared by doctor's `check_installed_settings_drift` and install.sh's
live-write step, with the per-daemon rationale beside each value. It runs from
`jasper-outputd` at −950 (the final DAC owner; killing it means silence) down
through the restartable accessory daemons at −300, `ssh` at −250, and two
positive entries the kernel should prefer to kill. Editing the unit files is a
separate step — the constants do not write them.

**Nothing operator-launched through SSH may inherit −1000**, which fully
disables OOM-kill for that PID. A root `python -` over SSH once inherited the
old `sshd=-1000` bias and survived while product daemons were killed around it;
−1000 is reserved for true system infrastructure. Open-ended Pi-side diagnostic
work goes through [`scripts/pi-run-diagnostic.sh`](../scripts/pi-run-diagnostic.sh),
which wraps `systemd-run` with memory/runtime bounds and a positive
`OOMScoreAdjust` so the kernel kills the diagnostic before the speaker.

**1b — zram at 50% of RAM with lz4** (`/etc/rpi/swap.conf.d/50-jts.conf`). The
Trixie default is ~100%, which amplifies thrash on a 1 GB Pi. zstd compresses
~30% better but decompresses ~3× slower per page on Cortex-A76; for a real-time
audio device predictable decompression latency beats compression ratio.

**1c — `vm.*` tuning** for low-RAM ARM with zram-only swap. Every knob and its
justification is annotated in
[`deploy/sysctl/99-jts-vm.conf`](../deploy/sysctl/99-jts-vm.conf), which owns
that rationale — don't restate it here. The one value not in the file is
`vm.min_free_kbytes`, computed per-Pi at install by `migrate_memory_resilience`
as `clamp(0.02 × MemTotal_kB, 8192, 262144)`: 2% is inside the safe band (>5%
causes OOM-immediate) and the 8 MB floor matches the Pi Foundation default and
must never be reduced. Doctor verifies the live value against the installed conf.

**1d — MGLRU `min_ttl_ms=1000`** (`deploy/tmpfiles/jts-mglru.conf`): protect any
page accessed in the last second from reclaim, even at the cost of triggering
OOM-kill instead. The most direct fix for the wedge shape. If anything
legitimate is killed under normal load, reduce to 500.

**Stage-1 drift detection**, all fail-soft (warn, not fail):
`check_installed_settings_drift` (one expected-value table naming each drifted
setting individually — the OOM ladder both as the unit files carry it and as
the live processes hold it, the restart policy, the vm.* sysctls, and MGLRU),
`check_zram_size_ratio` (WARN if zram > 60% of RAM), `check_memory_headroom`
(RAM-tier-aware: WARN below `max(100 MB, 10% × RAM)`, FAIL below
`max(30 MB, 3% × RAM)`). Disk pressure is separate:
`/state.resilience.disk` is the always-visible dashboard number, fail-soft to
`null`, while the graded thresholds (warn ≥85%, fail ≥95%) are owned by
`check_disk_space` in `jasper/cli/doctor/memory.py` so the two cannot drift.

### Stage 2 — the audio-protection subset (shipped)

Two changes, driven by a stress test that left the box alive but the music
audibly degraded (forensics in the historical appendix).

**1. Enable the memory cgroup controller.** `install.sh`'s
`migrate_cgroup_memory_enabled` idempotently removes any explicit
`cgroup_disable=memory` from `/boot/firmware/cmdline.txt`, then adds
`cgroup_enable=memory`, `cgroup_memory=1`, and `psi=1`. **Reboot required.**

**2. Carve audio and mic daemons into protected slices** with
`MemorySwapMax=0` + `ManagedOOMPreference=avoid`: `jts-audio.slice` (outputd,
fanin, camilla, camilla-crossover, shairport-sync, librespot, bluealsa-aplay,
and the bonded-grouping snapcast units) and `jts-mic.slice` (aec-bridge).
`Slice=` directives in the unit files — or drop-ins for units JTS does not own,
like `bluealsa-aplay.service.d/jts-slice.conf` — do the assignment. Once
`MemorySwapMax=0` is in effect the kernel literally cannot swap those pages to
zram: it keeps them in real RAM, sheds from unprotected cgroups, or OOM-kills
the audio daemon (a clean restart, preferable to silent jitter). The
full-profile installer enables and starts both slices after unit generation, so
neither relies on a later member start to exercise its `[Install]` path.

Slices rather than per-unit directives: a new audio daemon joins with one
`Slice=` line and policy lives in one place. Audio and mic are separate slices
because their failure modes differ (audible glitch vs missed wake events), which
a future oomd policy might want to treat differently.

Post-deploy verification (after the reboot) is one doctor line —
`sudo /opt/jasper/.venv/bin/jasper-doctor | grep -E "cgroup memory|audio path"`.
`check_cgroup_memory_enabled` reads `/sys/fs/cgroup/cgroup.controllers` (the
load-bearing signal: `/proc/cmdline` may still show a DTB-injected
`cgroup_disable=memory` alongside the enable tokens), and
`check_audio_path_no_swap` reads each audio daemon's `VmSwap` — meaningful swap
there means either `MemorySwapMax=0` is not enforcing (controller off, `Slice=`
unassigned) or pressure has already begun evicting audio pages.

**The rest of Stage 2 stays deferred** —
[ADR-0104](adr/0104-per-daemon-memory-caps-stay-deferred.md) names the four
triggers. Note that enabling the controller made the `MemoryHigh=`/`MemoryMax=`
directives already present in six unit files (mux, input, system-web,
bluetooth-web, librespot, and voice's `MemoryHigh=384M`) live: they now enforce
with values chosen while they were no-ops. `jasper-voice` has a throttle-only
`MemoryHigh` and no kill-cap; `jasper-control` (~35 MB) has no cap at all.

## Hardware-event recovery — sidebar to the ladder

Daemons that **exit cleanly at startup** because a USB device is absent are not
a hang and not a sibling-daemon problem: the dependency is physically missing.
`WatchdogSec` cannot help, and raising `StartLimitBurst` only delays the same
parked-failed outcome. Two parts answer it.

A udev rule on the dongle's USB IDs (`deploy/udev/99-jasper-apple-dongle.rules`,
using `SYSTEMD_WANTS` rather than `RUN+=` so systemctl dispatches via PID 1
asynchronously and udev's pipeline stays responsive) triggers
`jasper-dongle-recover.service` when Card A appears. That oneshot
`reset-failed`s the audio daemons, starts the output graph (`jasper-camilla`,
`jasper-outputd`, `jasper-audio-hardware-reconcile`), then best-effort starts
`jasper-aec-reconcile` where that mic/voice policy unit exists — streambox-safe,
since Zero-class boxes have output/DSP but no AEC brain. Idempotent, so rapid
replug is harmless.

`jasper-aec-reconcile` then owns the mic/AEC policy install-time detection could
not express. Its four cases:

- `JASPER_AEC_MODE=auto` + a profile-managed 6-channel XVF present → derive
  `JASPER_AEC_MIC_DEVICE`, set `JASPER_MIC_DEVICE=udp:<port>`, enable/start
  `jasper-aec-init` + `jasper-aec-bridge`, queue a `--no-block` voice restart.
- A configured direct mic candidate present but AEC unavailable → point
  `JASPER_MIC_DEVICE` at it, keep the bridge off, queue the same restart.
- No candidate mic present and the current value is one JTS owns (`Array`,
  `udp:<port>`, legacy `hw:N,1`) → clear the stale UDP back to the first
  candidate and stop voice so it does not watchdog-loop.
- A genuinely custom `JASPER_MIC_DEVICE` is left untouched — the escape hatch.

XVF identity lives in `jasper.mics.xvf3800`, exported by `jasper-xvf-profile`;
the reconciler is the single writer of `JASPER_AEC_MIC_DEVICE` for selectable
profiles. `JASPER_MIC_DEVICE_CANDIDATES` is a direct-mic fallback hint, not the
source of truth for XVF card identity.

## Wi-Fi profile recovery — sidebar to the ladder

Same shape, with the missing dependency being a file on the local filesystem.
Declarative reconciliation of state-vs-config drift, not liveness recovery. The
two incident classes it answers (a lost keyfile after an unclean shutdown, and
brcmfmac scan suppression that wedges scanning even while a profile is active)
are in the historical appendix.

The pieces: a **wizard-owned stash** at `/var/lib/jasper/wifi_guardian.env`
(mode 0600, `JASPER_WIFI_SSID` / `_PSK` / `_KEY_MGMT`), seeded at install by
`migrate_wifi_guardian`; a **pure-bash policy script**
`deploy/bin/jasper-wifi-guardian` → `/usr/local/sbin/`, run at boot by
`jasper-wifi-guardian.service` (`Type=oneshot`, after
`NetworkManager-wait-online`, `ConditionPathExists=` on the stash); a
**wizard helper** `jasper-wifi-scan-repair.service`, a root-only oneshot that
`/wifi/scan` starts through jasper-control's restart broker so `jasper-web`
stays cap-less; and **write hooks** in
[`jasper/web/wifi_setup.py`](../jasper/web/wifi_setup.py) that harden each
successful connect's NM profile (`autoconnect=yes`, `autoconnect-retries=0`
retry-forever, `802-11-wireless.powersave=2`, `ipv6.method=link-local` — which
keeps `.local` mDNS fast on Apple clients without routed IPv6, where `ignore`
leaves them waiting on IPv6 mDNS and reads as a stalled page load).

**The recovery timer** `jasper-wifi-recover.timer` runs every ~3 min with no
resident RAM and is deliberately **not** gated on the stash: active-link scan
repair does not need the PSK and must run on manually configured profiles too.
Steady state is one `nmcli connection show --active` read plus a narrow
recent-kernel-log check for `brcmf_cfg80211_scan: Scanning suppressed`, with
**no script output**. On a hit it runs
`python -m jasper.wifi_scan_repair --iface wlan0` even when NM reports an active
profile; with no active connection it calls the guardian only when the root-only
stash exists, else `event=wifi_recover.guardian_skip reason=no_stash`. Doctor's
`check_wifi_recover_timer` warns if the timer is disabled. Minutes, not seconds:
NM's retry-forever autoconnect already covers ordinary flaps, and the timer's
unique job is the rare scan-suppression wedge.

Guardian outcomes when invoked: SSID matches stash → `steady_state`; SSID
differs → `stash_stale` no-op (the operator switched networks; do not stomp a
working one); no active Wi-Fi but a profile for the stashed SSID exists →
`nmcli connection up` (`activate`, matching by profile name then by
`802-11-wireless.ssid`, so Imager/netplan names do not spawn duplicates); no
active Wi-Fi and no profile → the incident case, `nmcli dev wifi connect`
(`recreate_attempt`), deleting the broken half-profile and exiting non-zero on
failure so the operator notices.

**PSK redaction is enforced in three layers:** the bash script never passes
`$PSK` to `emit`/`log` and scrubs literal-PSK and `password \S+` patterns from
nmcli stderr; the Python hooks log only SSID + `key_mgmt`; `/state` and doctor
read the stash for SSID and never emit the PSK — `/state` is unauthenticated on
the LAN and doctor output ends up in install transcripts.

```sh
journalctl -u jasper-web -u jasper-wifi-recover -u jasper-wifi-guardian \
  | grep -E 'event=(wifi\.(connect|forget|radio|post_dispatch_failed)|wifi_(recover|guardian))'
curl -s http://jts.local:8780/state | jq .resilience.wifi_guardian
sudo /usr/local/sbin/jasper-wifi-recover --reason manual   # full down-path nudge
sudo /usr/local/sbin/jasper-wifi-guardian --reason manual  # guardian only
```

## Implementation map

- `jasper/watchdog.py` — the `Heartbeat` sentinel; graceful no-op when
  `NOTIFY_SOCKET` is unset, so daemons run under `python -m` in development.
- `jasper/cli/aec_bridge.py` — `_aec_loop` bumps the heartbeat per processed
  frame and keeps `BridgeStalled` as an explicit mic-empty detector: a
  continuous-empty counter (`JASPER_AEC_STALL_RESTART_SEC`, 5 s) plus a
  slow-drip frame-rate watchdog (`JASPER_AEC_STALL_DRIP_MAX_WINDOWS`) for the
  intermittent trickle the continuous counter resets through — the first never
  fired during a ~13 h deaf-but-trickling episode.
- `jasper/voice_daemon.py` — `WakeLoop` bumps at the top of each `run()`
  iteration. On a speaker with no room mic (#2205) that loop iterates a fixed
  keepalive tick, so the bump proves the loop is alive but says nothing about
  input; see HANDOFF-hotplug-resilience.md and #2243.
  `jasper/audio_io.py` — `UdpMicCapture`, `parse_udp_device`,
  `make_mic_capture`; queue init is deferred to `__aenter__` so the classes are
  construct-safe from sync code.
- `jasper/control/supervisor_runtime.py` — shared mechanics for the shairport,
  grouping, and system supervisors: cold-start/jitter cadence, per-tick crash
  isolation, env mode normalization, disabled snapshots, asyncio-thread
  lifecycle. Subsystem policy, singleton ownership, and event names stay local.
  `shairport_supervisor.py` is the reference adapter: `run()` is the subsystem
  seam, `_tick()` the pure policy under test.
- `deploy/bin/jasper-audio-hardware-reconcile` (+ unit and udev rule) — the same
  event-driven shape for output DAC roles, and `deploy/bin/jasper-outputd-failure-reconcile`,
  outputd's `ExecStopPost=` hook that parks repeated `EX_CONFIG=78` exits and a
  four-times-failing content-lane open instead of looping into
  `StartLimitAction=reboot`. Both are owned by
  [HANDOFF-hotplug-resilience.md](HANDOFF-hotplug-resilience.md); the one thing
  that belongs here is that the park record
  (`/run/jasper-outputd-content-lane.state`) has ONE reader,
  `jasper/control/content_lane_state.py`, feeding two surfaces that therefore
  cannot disagree: `/state.resilience.content_lane` and doctor's
  `check_outputd_content_lane_park`, which FAILs on a park and repeats the
  record's lane-specific `action` verbatim.
- `deploy/modprobe.d/snd-aloop.conf` — single-card config
  (`enable=1 index=6 id=Loopback pcm_substreams=8`).
- Tests: `tests/test_watchdog.py` (sentinel contract),
  `tests/test_udp_mic_capture.py` (receiver contract),
  `tests/test_aec_reconcile.py` (stale-UDP and hardware modes),
  `tests/test_shairport_supervisor.py` (threshold/gate/rate-limit/degradation
  plus the default RTSP probe against a real asyncio server).

## Verification on a running Pi

`systemctl show jasper-aec-bridge -p Type -p WatchdogUSec -p Restart -p
TimeoutStopUSec` returns `Type=notify Restart=on-watchdog TimeoutStopUSec=5s
WatchdogUSec=30s` (same for `jasper-voice`); `systemctl show -p
RuntimeWatchdogUSec` returns `1min` with `WatchdogDevice=/dev/watchdog0`;
`sudo journalctl --header | grep "File path"` shows `/var/log/journal/...`, not
`/run/log/journal/...`; `curl -s localhost:8780/state | jq .resilience.shairport`
returns `enabled=true` with a recent `last_probe_at` once the 60 s cold start has
elapsed. With the selected XVF card absent,
`journalctl -u jasper-aec-reconcile -e` shows stale owned UDP state cleared, the
bridge disabled/inactive, and voice stopped rather than watchdog-looping.

```sh
# The watchdog actually catches a wedge:
sudo kill -STOP $(pgrep -f jasper-aec-bridge | head -1)
sudo journalctl -fu jasper-aec-bridge
# within 30 s: "Watchdog timeout (limit 30s)!" → SIGABRT → restart

# The dongle-recovery udev rule fires: pull the speaker cable from the
# dongle's 3.5 mm jack until /proc/asound/cards drops Card A, then re-seat.
sudo journalctl -fu jasper-dongle-recover
# within 1-2 s: ExecStart= lines, then a clean exit; jasper-camilla and
# jasper-voice both read `active` afterward regardless of prior state.
```

## What we explicitly did NOT do

A **generic third-party-daemon supervisor framework**: Tier 3's shape is
reusable, but shairport is the only daemon that has demonstrated the failure
class, and lifting it before a second instance buys complexity, not value.
**Tier 4 today**: kernel-state recovery waits on evidence of need. And the
alternatives ADR-0102 rejected — a PipeWire migration (scoped to this question;
HANDOFF-barge-in.md re-opens it honestly as a costed option when robust barge-in
is the motivation), in-process AEC, and a separate watchdog daemon.

Last verified: 2026-08-25 (triage pass — unit and script paths, doctor check
names, slice membership, the OOM ladder, and supervisor mechanics rechecked
against `deploy/systemd/`, `deploy/bin/`, `jasper/_oom_adj.py`,
`jasper/cli/doctor/`, and `jasper/control/`. Incident narratives and the
never-shipped Stage 2 architecture moved to
`docs/historical/resilience-incidents-2026-05.md`; three decisions extracted to
ADR-0102/0103/0104.)
