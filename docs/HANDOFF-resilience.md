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

Even with the snd-aloop failure class eliminated, we want
recovery from *any* future in-process hang — not just the
specific one we hit. The systemd `sd_notify` watchdog gives us
that generically.

| Tier | Mechanism | Catches | Wired? |
|---|---|---|---|
| 1 | `sdnotify` heartbeat thread with progress sentinel | Logic deadlock; blocked event loop; slow loop. `bump()` is called from each successful frame; the heartbeat thread only pats systemd if `now - last_progress < 5 s`, so a wedged loop stops patting even though the heartbeat thread itself keeps running. | ✅ — `jasper/watchdog.py`, wired into bridge `_aec_loop` and voice `WakeLoop.run` |
| 2 | systemd `Type=notify` + `WatchdogSec=30s` + `Restart=on-watchdog` + `TimeoutStopSec=5s` + `StartLimitBurst=20` | Process exit, hang, fatal ALSA error. If the Tier 1 heartbeat stops patting, this fires at 30 s and brings the daemon back with a fresh process in ~2 s. | ✅ — `deploy/systemd/jasper-aec-bridge.service`, `deploy/systemd/jasper-voice.service` |
| 3 | `OnFailure=jasper-recover@%n.service` chained to a templated recovery unit that escalates after `StartLimitBurst` is exhausted | Dependent-daemon failures, repeated restart loops. Useful when restarts don't succeed because a sibling is broken (e.g. camilla wedged, bridge keeps failing). | ❌ — not currently wired. May become unnecessary now that UDP eliminates the main "siblings break each other" case. Deferred pending evidence of need. |
| 4 | Kernel-state recovery script (`rmmod && modprobe snd_aloop`, after stopping all consumers; rate-limited via state file) | snd-aloop kernel-side wedges, dsnoop wedges. | ❌ — not currently wired. The original motivation (bridge↔voice snd-aloop) is gone; the music-chain Loopback hasn't shown this failure mode in production. Deferred. |
| 5 | BCM2712 hardware watchdog (`/dev/watchdog0`) patted by systemd PID 1 via `RuntimeWatchdogSec=15s` in `/etc/systemd/system.conf.d/` | Kernel panic, PID 1 hang, total system wedge. | ❌ — not currently wired. The Pi 5's `bcm2712_wdt` driver is available in the 64-bit kernel; enabling is a one-line config + reboot. Worth doing once Tiers 1+2 have soaked. |

The honest framing: today's shipped resilience is **Tier 1, Tier 2, and the architectural choice that obviated Tiers 3–4 for the AEC path.** Tier 5 is cheap and worth doing; Tiers 3–4 are kept on the deferred list with a clear trigger ("do this if we hit a wedge that the systemd watchdog can't fix").

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
- `deploy/install.sh:enable_aec_if_compatible` — auto-detects 6-ch
  firmware, sets `JASPER_MIC_DEVICE=udp:9876`, enables the bridge
  services. Includes the legacy-value migration: if an existing
  install has `JASPER_MIC_DEVICE=hw:N,1` (the pre-UDP sentinel),
  it gets auto-flipped on the next install run.
- `tests/test_watchdog.py` — sentinel-contract tests
  (fresh/stale/recovery/disabled-fallback).
- `tests/test_udp_mic_capture.py` — UDP receiver contract
  (parse-forms, factory dispatch, end-to-end frame yield).

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

Smoke test that the watchdog actually catches a wedge:

```sh
sudo kill -STOP $(pgrep -f jasper-aec-bridge | head -1)
sudo journalctl -fu jasper-aec-bridge
# within 30 s: "Watchdog timeout (limit 30s)!" → SIGABRT → restart
```

---

## What we explicitly did NOT do, and why

- **PipeWire migration.** Out of scope per project policy. The
  resilience win comes from removing snd-aloop from the
  bridge↔voice path entirely, not from replacing the userspace
  audio stack.
- **In-process AEC** (embed `jasper_aec3` directly in
  `jasper-voice`). Simpler but expands voice's blast radius — a
  crash in AEC3 today takes down only the bridge; in-process it
  would take down wake-word too. UDP preserves the isolation.
- **A separate watchdog daemon** (monit, supervisord, custom).
  systemd's built-in `sd_notify` + `Restart=on-watchdog` does
  everything those would, with no new process.
- **Tier 3–5 today.** Each has a clear trigger condition for
  when to wire it. Shipping Tiers 1+2 first lets us see what
  actually fails next before adding more machinery.

---

## References

- [Crash-Only Software, Candea & Fox (HotOS-IX 2003)](https://www.usenix.org/legacy/events/hotos03/tech/full_papers/candea/candea.pdf) — the conceptual frame.
- [Lennart Poettering, "systemd for Administrators, Part XV: Watchdogs"](http://0pointer.de/blog/projects/watchdog.html) — sd_notify + WatchdogSec design.
- [sd_notify(3) man page](https://www.freedesktop.org/software/systemd/man/latest/sd_notify.html).
- [sound/drivers/aloop.c on torvalds/linux](https://github.com/torvalds/linux/blob/master/sound/drivers/aloop.c) — the `loopback_cable` state machine that wedges on SIGKILL.
- [ALSA C library — PCM Interface (snd_pcm_recover, states)](https://www.alsa-project.org/alsa-doc/alsa-lib/group___p_c_m.html) — why `DISCONNECTED` is unrecoverable.
- PRs that shipped this design: [JTS#77](https://github.com/jaspercurry/JTS/pull/77) (Tier 1+2 watchdog), [JTS#93](https://github.com/jaspercurry/JTS/pull/93) (UDP transport + LoopbackAEC retirement).
