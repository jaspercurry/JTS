# Resilience incidents and deferred analysis (2026-05) — historical

> **Status: historical.** Frozen record of the incidents that shaped the
> resilience ladder and the Stage 2 memory analysis that has not been picked
> up. Numbers describe the box and topology of their stated date. Current
> operational truth is [HANDOFF-resilience.md](../HANDOFF-resilience.md).

## 2026-05-11 — the snd-aloop kernel wedge

`jasper-aec-bridge`'s mic-side PortAudio `InputStream` stopped invoking its
callback after a USB underrun on the XVF chip's UAC2 capture endpoint. The
main thread was blocked in `out_stream.write()` to the old `hw:LoopbackAEC,0`
snd-aloop card and never observed Python's `SIGTERM` handler — signal
handlers only run between bytecodes, and a blocked C call holding the GIL is
opaque to them.

systemd waited the default 90 s `TimeoutStopSec`, then sent `SIGKILL`. That
killed the bridge mid-flight while it held the loopback fd open, leaving
snd-aloop's kernel-side `loopback_cable` struct half-bound: the timer that
advances `hw_ptr` never re-armed. Every fresh bridge opened the card
successfully and blocked forever on its second write. Only
`rmmod snd_aloop && modprobe snd_aloop` — after stopping all six consumers
(`shairport-sync`, `librespot`, `bluealsa-aplay`, `jasper-camilla`,
`jasper-aec-bridge`, `jasper-voice`) — or a reboot recovered. The wake path
was silently dead for ~10 minutes; no cue fired, because cues are gated on a
wake event and wake events need the mic that was missing.

Three classes of fragility composed:

1. PortAudio's `InputStream` is one-shot once the PCM reaches
   `SND_PCM_STATE_DISCONNECTED` — `snd_pcm_recover()` does not recover that
   state per the ALSA contract.
2. Blocking I/O in a Python daemon defeats `SIGTERM`.
3. `SIGKILL` of an snd-aloop consumer corrupts kernel state that survives
   process restarts — structural in aloop's design.

The response was [ADR-0102](../adr/0102-mic-transport-is-udp-localhost.md)
(UDP localhost transport) plus the sd_notify watchdog ladder.

Same day, a second shape: with nothing plugged into the Apple dongle's 3.5 mm
jack the dongle drops its UAC interfaces, so a power-cycle left the Pi booted
with the dongle enumerated but no Card A. `jasper-camilla`,
`jasper-aec-bridge`, and `jasper-voice` each failed to open `hw:CARD=A`,
exited 1, hit `StartLimitBurst` after ~5 attempts, and parked failed. Nothing
watched for the card reappearing, so re-seating the cable left the speaker
silent until a manual `reset-failed && start`. `WatchdogSec` cannot help — the
daemons exited cleanly. The fix was the udev-triggered
`jasper-dongle-recover.service` plus the mic/AEC reconciler.

## 2026-05-19 — shairport-sync's AP2 protocol wedge

shairport-sync v4.3.7's AP2 control plane occasionally hangs after `accept()`
on a per-connection RTSP handshake. The process stays alive, mDNS still
advertises AirPlay, MPRIS still answers `PlaybackStatus`, and systemd sees
nothing to restart — but every new SETUP times out. The closest upstream
report is [shairport-sync#2024](https://github.com/mikebrady/shairport-sync/issues/2024),
where `strace` showed the listener thread stuck in `pselect6`; closed without
a code fix. Restarting the unit resolves it. This is the failure class Tier 3
exists for.

A later hardware observation (2026-07-10) added the disabled-unit guard: the
supervisor revived a deliberately disabled AirPlay unit ~90 s after the
`/sources/` toggle, because the "unit inactive" gate bypass could not tell
"crashed" from "turned off".

## 2026-05-20 — the watchdog with no journal

Raspberry Pi OS Trixie ships both `RuntimeWatchdogSec=1m` (hardware watchdog
armed) and `Storage=volatile` (journal discarded on reboot, to protect the SD
card from log-write wear). They compose into a debuggability hole: wedge →
watchdog reset → fresh boot → no record of what wedged.

The fix (PR #160) flipped journald back to `Storage=persistent` with a
`SystemMaxUse=` cap. SD wear cost is ~30 MB/hour with ZSTD compression,
~270 GB/year — well inside any reasonable card's ~100 TBW endurance. Swap is
on `zram0`, not the card, so OOM events do not thrash it: the wear the
volatile default hedged against was the wrong threat for this topology.

Forensics after a watchdog-triggered reset:

```sh
sudo journalctl --list-boots
sudo journalctl -b -1 -p warning --since "-2min"
# EXT4 fingerprint of an unclean shutdown, seen on the *recovery* boot:
sudo dmesg -T | grep "orphan cleanup"
```

## 2026-05-23 — userspace dead, watchdog satisfied

A PIO compile on the 1 GB Pi 5 OOM-stalled userspace for over two minutes:

- ICMP ping stayed healthy (~7 ms RTT, 0% loss) — kernel and network alive.
- `ssh` accepted at the TCP layer but the **banner exchange timed out**.
- **No watchdog reset** — PID 1 got just enough scheduler time to keep patting
  `/dev/watchdog0` every <60 s.
- Recovery required a manual power-cycle.

The gap: systemd patting `/dev/watchdog0` is a very weak liveness signal. It
confirms only that PID 1's main loop got CPU once in the last 60 s — not that
sshd accepts connections, that jasper-control answers HTTP, or that any
user-visible service does anything. Userspace can be fully wedged while the
hardware watchdog thinks the system is healthy.

That analysis produced T5.1 (`StartLimitAction=reboot` on the critical units)
and T5.2 (`SystemSupervisor` probing sshd's banner, `/healthz`, and
`/proc/loadavg`), plus Stage 1 memory-pressure prevention. Heavy *offline*
analysis on the Pi — e.g. instantiating `openwakeword.Model()` 100 times in a
sweep script, each load holding 100–200 MB — is a known way to trip this
shape self-inflicted; that is what `scripts/pi-run-diagnostic.sh` exists for.

## 2026-05-24 — the stress test that bought the audio slices

`stress-ng --vm 1 --vm-bytes 300M --vm-keep --timeout 60s` on the 1 GB Pi 5
with Stage 1 + T5.1 + T5.2 in place. The system *survived*: load capped at
3.07, SystemSupervisor probes stayed green, all six jasper-* daemons stayed
active, and the OOM killer never fired — zram absorbed the pressure.

**But the music played during the stress was audibly degraded** — "splotchy,
crushed" per the operator's real-time report. Forensics immediately after:

```
jasper-aec-bridge: VmLck=16 kB    VmSwap=43056 kB   ← 42 MB in zram
jasper-camilla:    VmLck=64 kB    VmSwap=416 kB
```

Mechanism: under pressure the kernel evicted audio-path daemons' pages to
zram; subsequent audio-frame access triggered decompression (~10–15 µs per
page on Cortex-A76) whose latency variance exceeded the ALSA buffer's ~10 ms
slack. `LimitMEMLOCK=infinity` grants permission to lock memory but the
daemons were not calling `mlockall()` — only ~64 kB was locked.

That is exactly what `MemorySwapMax=0` on a cgroup prevents, and it is why
the audio-protection subset of Stage 2 shipped the same day.

## Stage 2's full architecture (never shipped)

The deferred remainder, kept so the next contributor does not re-derive it.
The decision and its triggers are
[ADR-0104](../adr/0104-per-daemon-memory-caps-stay-deferred.md).

The proposal was purpose-named slices carrying policy declaratively:

```
jts-audio.slice    ← fanin, camilla, shairport-sync, librespot, bluealsa
                     MemorySwapMax=0          # audio pages NEVER touch zram
                     ManagedOOMPreference=avoid
jts-mic.slice      ← aec-bridge
                     MemorySwapMax=0
jts-control.slice  ← control, mux, input, wiim-remote-mic
                     MemoryHigh=120M MemoryMax=180M
jts-voice.slice    ← voice
                     MemoryHigh=220M MemoryMax=320M
                     ManagedOOMMemoryPressure=kill
jts-wizard.slice   ← web, system-web, bluetooth-web, correction-web
                     MemoryHigh=64M MemoryMax=128M
                     ManagedOOMMemoryPressure=kill
```

The audio and mic halves shipped. The rest would add three failure classes
Stage 1 + T5.x do not catch: a slow leak in a single daemon (biased away from
by `OOMScoreAdjust` but not capped); audio jitter from zram decompression on
the non-audio path; and `systemd-oomd` slice-level kills that are more
targeted than the kernel's badness heuristic.

Estimated cost at the time: ~8 MB of kernel-side accounting on a 1 GB Pi
(~0.8%), ~15 MB for oomd, ~2–3 hours of engineering, one reboot. The
moderate risk was that the existing `MemoryHigh`/`MemoryMax` values become
effective — they were sized while they were no-ops, so nobody knows whether
`MemoryMax=120M` on `jasper-mux` is right or generous.

Known concerns with `systemd-oomd` on Pi-class hardware: the cgroup
enablement quirks in
[HANDOFF-tier5-watchdog-liveness.md](../HANDOFF-tier5-watchdog-liveness.md)
Option E, and the "kills the whole cgroup with no per-process forensics"
complaint from Fedora's 34-era rollout.

## Wi-Fi profile loss and the brcmfmac scan wedge

**2026-05-23.** USB-C power was yanked during a power-splitter swap while the
root ext4 partition had an in-flight write to
`/etc/NetworkManager/system-connections/<SSID>.nmconnection`. Journal
recovery on the dirty mount discarded the partially-written file entirely, so
the Pi came up with **no** Wi-Fi profile at all, unreachable on the LAN.
Recovery took ~1 hour with HDMI and a USB keyboard.

**2026-06-19 (JTS3).** The AP flapped several times; the Pi 5 brcmfmac driver
logged repeated `brcmf_cfg80211_scan: Scanning suppressed: status (4)` and
NetworkManager eventually stopped retrying the seeded netplan profile after a
`no-secrets` failure. A power cycle brought it back.

**2026-06-26 (JTS).** The nastier sibling: NetworkManager still reported an
active profile while the brcmfmac scan path was wedged, and the box
disappeared from `jts.local`.

Together these produced the guardian (stash + pure-bash policy script) and
the low-footprint recovery timer that runs the bounded scan-suppression
repair even while a profile is active. Explicitly out of scope, per PR #266:
an NM dispatcher script on `up` events (the wizard hooks already cover the
asymmetry), a multi-network stash (the speaker does not travel), and
WPA-Enterprise.

Why a custom guardian rather than NM-native restoration: there is no native
path. NetworkManager has no documented "restore profile from backup"; the
pattern people roll themselves is a dispatcher script plus a sidecar config
store, which is the shape of this guardian.

## References

- [Crash-Only Software, Candea & Fox (HotOS-IX 2003)](https://www.usenix.org/legacy/events/hotos03/tech/full_papers/candea/candea.pdf)
  — the conceptual frame: design every component so the only stop path is
  crash-and-recover, exercise that path constantly, prefer micro-reboots.
- [Lennart Poettering, "systemd for Administrators, Part XV: Watchdogs"](http://0pointer.de/blog/projects/watchdog.html)
  and [sd_notify(3)](https://www.freedesktop.org/software/systemd/man/latest/sd_notify.html).
- [sound/drivers/aloop.c](https://github.com/torvalds/linux/blob/master/sound/drivers/aloop.c)
  — the `loopback_cable` state machine that wedges on SIGKILL.
- [ALSA PCM interface](https://www.alsa-project.org/alsa-doc/alsa-lib/group___p_c_m.html)
  — why `DISCONNECTED` is unrecoverable.
- PRs that shipped the original design: JTS#77 (Tier 1+2 watchdog), JTS#93
  (UDP transport + LoopbackAEC retirement), JTS#160 (persistent journal),
  JTS#266 (Wi-Fi guardian), JTS#573 (bootloop guard).
- [historical/RUNBOOK-2026-06-10-batch-hardware-validation.md](RUNBOOK-2026-06-10-batch-hardware-validation.md)
  — the bootloop-guard hardware validation run.
