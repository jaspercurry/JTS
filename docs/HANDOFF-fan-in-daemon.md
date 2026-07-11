# Handoff: fan-in renderer topology

> **Status: production default and only supported renderer topology as
> of 2026-05-26 (PR #329 plus follow-up cleanup).** The dmix/fanin
> switcher and `/var/lib/jasper/audio_topology.env` are retired.
> install.sh writes the fan-in `/etc/asound.conf` directly, enables
> `jasper-fanin.service`, and archives/removes stale topology state.
> Original Phase 1/2 work (daemon source, systemd unit, install.sh
> build wiring, jasper-doctor checks, `/state` aggregation) landed in
> PR #308 on 2026-05-25; PR #329 promoted fan-in after the AirPlay
> Pattern A3 investigation and set the 4096-frame input buffer.

## Why the cutover (2026-05-26)

The dmix topology PR #214 introduced (2026-05-22) was working at the
"can multiple renderers play without crashing" level, but on-Pi
measurement on 2026-05-26 found it was producing **~5-7 audible drops
per minute on AirPlay** (Pattern A3 in docs/HANDOFF-airplay.md). The
mechanism turned out to be a layered interaction we hadn't
anticipated:

1. **802.11 A-MPDU aggregation** delivers AirPlay 2 RTP packets in
   **bursts of ~4 packets every ~30 ms** instead of the nominal 1
   packet every ~8 ms. Confirmed via tcpdump capture of the
   Mac Studio → Pi flow.
2. **shairport faithfully wrote those bursts** into the old
   `pcm.jasper_renderer_in` (plug wrapper on the dmix).
3. **The dmix's per-write mutex / context-switch** was slipping
   shairport's player thread by ~5 ms when it computed
   `should_be_frame` for the next packet — just enough to push the
   head packet of each burst past the hardcoded
   `desired_lead_time=0.120 s` threshold in shairport-sync v4.3.7's
   `player.c:1130` check.
4. Every burst whose head-packet processing slipped into the wrong
   half of the threshold got dropped as "out of date."

**Switching to fanin eliminated the drops entirely**: 0 drops in 5
min vs the dmix's 55 drops in the prior 10 min on the identical Mac
Studio sender + same WiFi link. fanin replaces the userspace dmix
with per-renderer snd-aloop substreams + a Rust summing daemon — no
shared dmix mutex, no shared write-timing perturbation. The
validation A/B is documented in docs/HANDOFF-airplay.md Pattern A3
"The fanin verdict (2026-05-26)."

The cutover also required bumping the per-input ALSA buffer (see
"Configuration" below) because the dmix layer was incidentally also
the WiFi-burst absorption layer.

## TL;DR

JTS's current music topology gives each renderer (librespot,
shairport-sync, bluealsa-aplay, USB-in, future HDMI) a private
snd-aloop lane. The prior userspace renderer dmix
(`pcm.jasper_renderer_mix`, added 2026-05-22 by PR #214) solved
multi-writer `-EBUSY` but added ~85 ms of buffering and, more
importantly, introduced AirPlay burst-timing drops. The fan-in daemon
keeps the single summed music reference without the shared dmix writer
path.

Each renderer gets its own snd-aloop substream (`hw:Loopback,0,0..3`),
and correction/test playback gets `correction_substream`
(`hw:Loopback,0,4`). A small Rust daemon (`jasper-fanin`) reads the
capture side of each substream, sums them sample-wise, and writes to a
single dedicated
"summed music" substream (`hw:Loopback,0,7`). CamillaDSP and the AEC bridge
both dsnoop on the capture side of that summed substream (`hw:Loopback,1,7`)
— same consumer shape, just one substream pair shifted from the old
dmix tap. The renderer dmix layer and its ~85 ms of latency disappear;
the AEC reference signal becomes cleaner (post-mix, single point of
truth); adding a future HDMI source is
"assign the next free private substream" rather than "fight contention."

Net latency saving over the retired dmix topology: ~85 ms of
renderer-side queueing. With the current 1024-frame fan-in output queue,
the fixed downstream delay shairport must compensate is 1024 frames
smaller than the old 3072-frame default. The saving survives any future
PipeWire migration.

## 2026-06-29 JTS2 output-buffer retune

JTS2 (Pi 5, Apple USB-C dongle DAC, AirPlay source) was live-retuned after
the DAC-latency-floor work moved CamillaDSP to `chunksize=256`,
`target_level=1536` and outputd to `period=256`, DAC buffer `512`.

Findings:

- `JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1024` is stable on this path. Initial
  listening and counter watch were clean, and a post-rollback 3-minute
  monitor showed zero new fan-in input/output xruns, zero outputd content
  xrun/empty/partial/EAGAIN deltas, zero DAC xruns, zero Camilla playback
  underruns, and zero shairport underruns.
- `JASPER_FANIN_OUTPUT_BUFFER_FRAMES=512` is not stable. It failed within
  about 40 seconds: fan-in reported `output.buffer_frames=512` and
  `output.xrun_count=2876`. That is a hard no for production defaults on
  this hardware path.
- The 1024 result is an audio-stability finding, not a video-sync proof.
  The user observed AirPlay video lip-sync problems from the computer even
  after the 1024 audio counters were clean. Keep A/V sync validation
  separate from xrun/underrun validation.

So the production floor for the loopback fan-in output buffer is 1024
frames (~21.3 ms at 48 kHz). Sub-1024 values remain lab-only and must not
be codified without a new hardware soak and A/V sync check.

For future playback sources, this doc owns the topology details, but
the cross-cutting contributor checklist lives in
[`audio-paths.md`](audio-paths.md#adding-a-new-music-source). Start
there so lane assignment, mux, volume, doctor, source wizard, and
measurement-window updates happen together instead of drifting into
parallel one-off lists.

## Why now

PR #214 (2026-05-22) solved a real `-EBUSY` crash-loop with the smallest
change available — a userspace dmix. The fix was correct for the bug it
addressed but had two unanticipated consequences:

1. **Invisible latency.** The dmix's 4096-frame internal buffer between
   client writes and slave reads is not reported by `snd_pcm_delay()`,
   which made shairport's AirPlay scheduling math undercount by ~85 ms.
   Tier 1A fixed this by updating the latency offset derivation to know
   about the dmix; the artifact symptom is gone, but the topology is
   still carrying the buffer.

2. **Implicit EBUSY arbitration disappeared.** Before the dmix, snd-aloop's
   single-writer contract forced one-at-a-time renderer ownership of the
   loopback; `-EBUSY` was the kernel's enforcement of "latest source wins"
   even when `jasper-mux`'s explicit pause path failed. After the dmix,
   nothing prevents two renderers from mixing audibly. PR #216 added a
   Tier 2 escalation (`systemctl restart librespot`) to compensate, but
   it's a workaround for a property the topology used to provide for free.

The structural insight that wasn't obvious at the time: **snd-aloop has 8
substream pairs, each with independent rate state, format state, and
single-writer enforcement.** Each pair is a fully independent virtual
cable. Assigning each renderer to its own substream gets back the
`-EBUSY` arbitration (per-pair single-writer), eliminates the rate-lock
footgun (per-pair rate declaration), and lets us delete the dmix layer.
Verified in `sound/drivers/aloop.c` upstream: `cable = loopback->cables[substream->number][dev]`
allocates per-substream cable structs with their own `loopback_pcm_hardware`,
timer ops, atomic stop counter, spinlock, and wait queue. No shared cable
state across substreams.

The remaining challenge is the capture side: CamillaDSP wants one capture
device. That's the fan-in daemon's job.

## The topology

```
Renderers (each on its own snd-aloop substream pair):
  librespot            → hw:Loopback,0,0
  shairport-sync       → hw:Loopback,0,1
  bluealsa-aplay       → hw:Loopback,0,2
  jasper-usbsink       → hw:Loopback,0,3
  correction/test      → hw:Loopback,0,4
  (reserved)           → hw:Loopback,0,5    [debug/monitor mirror, for offline AEC capture]
  (reserved)           → hw:Loopback,0,6

jasper-fanin (the new Rust daemon):
  reads from           ← hw:Loopback,1,0..4 (via per-substream dsnoop or direct hw)
  sums sample-wise
  writes to            → hw:Loopback,0,7

The "summed music" substream:
  CamillaDSP captures  ← pcm.jasper_capture → dsnoop on hw:Loopback,1,7
  AEC fallback/diag    ← pcm.jasper_ref     → dsnoop on hw:Loopback,1,7
  Production AEC ref   ← outputd UDP speaker monitor after CamillaDSP/outputd

CamillaDSP → outputd_content_playback → jasper-outputd → Apple USB-C dongle
```

### What this preserves

- **Pre-DSP TTS for every output profile.** TTS/cues enter
  `jasper-fanin` instead of bypassing CamillaDSP. Fan-in measures
  pre-duck program loudness, applies the provider/profile peak-capped
  assistant gain policy, applies program ducking to renderer lanes, then
  mixes TTS/cues before CamillaDSP crossover/protection.
- **AEC fallback tap shape.** `pcm.jasper_ref` remains a plug-wrapped
  dsnoop for explicit fallback/diagnostics. The normal production AEC
  reference is outputd's UDP speaker monitor after CamillaDSP/outputd.
- **Mux arbitration.** `jasper-mux` still owns source policy:
  latest-source-wins in auto mode, and user-selected source override
  from the landing page. Fan-in only enforces the low-level selected
  input gate when mux asks it to.
- **Renderer service files.** Each renderer's `--device` flag changes
  from `jasper_renderer_in` (the plug-on-dmix) to its assigned substream
  alias (`pcm.librespot_substream`, etc.). The renderer code is unchanged.
- **Final output owner.** Changed by the outputd mainline topology:
  the already-crossed-over/protected stream reaches `jasper-outputd`,
  and `pcm.jasper_out` is only the pre-outputd rollback dmix.
- **CamillaDSP config.** Capture device stays `plug:jasper_capture`.
  The dsnoop's underlying substream remains `(1,7)` in the asoundrc —
  invisible to CamillaDSP itself. Playback is `outputd_content_playback`
  in the outputd topology.

### What this deletes

- `pcm.jasper_renderer_mix` dmix block in asoundrc — gone.
- `pcm.jasper_renderer_in` plug wrapper — gone.
- The renderer-side 85 ms buffer that was invisible to shairport.
- The need for shairport's `audio_backend_latency_offset_in_seconds` to
  carry a renderer-dmix term. The current derivation compensates
  CamillaDSP's `target_level` above `chunksize`, the fan-in output
  buffer, and outputd's DAC buffer, so the cutover offset is
  `-0.106667` with generic `target_level: 2048`, fan-in output buffer
  `1024`, and outputd DAC buffer `3072`; on JTS2's low-latency Apple
  profile (`256/1536`, fan-in `1024`, outputd DAC `512`) it is
  `-0.058667`.

### What this adds

- One new Rust daemon (`jasper-fanin`), ~5 MB RSS, <2% of one Pi 5 core
  steady-state.
- One new systemd unit (`jasper-fanin.service`).
- Per-renderer asoundrc PCM aliases (so renderers can name their
  substream without hardcoding `hw:Loopback,0,N` everywhere).
- A modprobe.d pin: `pcm_substreams=8 pcm_notify=0 enable=1 index=6` to
  make snd-aloop's substream count and card index deterministic.

## Daemon design

### Resource budget

- **RSS: ≤8 MB.** Rust with `alsa-rs`. Validated by `jasper-doctor` and
  `cat /proc/$(pidof jasper-fanin)/status | grep VmRSS`.
- **CPU: <2% of one core steady-state.** Workload is N × (ALSA read) +
  saturating-add sum + 1 × (ALSA write) at 48 kHz S16_LE stereo. Per
  frame: ~256 sample-pair additions per period, trivial.
- **Disk: append-only `/var/lib/jasper/fanin/xrun_history.jsonl`, ring of
  last 100 events.** Atomic append. Bounded to ~10 KB.

### Real-time scheduling

The daemon's work loop runs at `SCHED_FIFO` priority 30 with `mlockall(MCL_CURRENT | MCL_FUTURE)`.
OSPERT 2024 measured Pi 5 stock-kernel worst-case scheduling latency at
36.8 ms under heavy stress; SCHED_FIFO + mlockall reduces this materially
(though not to PREEMPT_RT levels). Whether to escalate to PREEMPT_RT is
**gated on the post-merge soak**: if xruns appear under realistic stress
(simultaneous Spotify + AirPlay handover + TTS + WiFi storm), escalate
and document as a hard requirement. Otherwise stock kernel is the floor
for community contributors testing on their own hardware.

### Heartbeat (Tier 1 + Tier 2 resilience)

The daemon implements JTS's standard progress-sentinel watchdog pattern
(documented in `jasper/watchdog.py:Heartbeat`):

- A single atomic `LAST_PROGRESS_NS` updated by the work loop after **every
  successful frame** processed (read N inputs, sum, write 1 output).
- A heartbeat thread wakes every 10 s and calls `sd_notify(false, &[NotifyState::Watchdog])`
  **only if** `now - LAST_PROGRESS_NS < 5 s`. If the work loop wedges,
  pings stop, systemd's `WatchdogSec=30 s` expires, and `Restart=on-failure`
  brings the daemon back fresh in ~2 s.
- The work loop must NEVER call `sd_notify` directly. The whole point of
  the sentinel is that liveness is gated on real forward progress, not on
  whether the heartbeat thread happens to be running.

`TimeoutStopSec=5 s` (not the systemd default 90 s) is load-bearing —
matches the 2026-05-11 snd-aloop wedge lesson: a daemon with blocked I/O
must escalate to SIGKILL fast so the kernel-side ALSA state doesn't get
corrupted by waiting on a hung consumer.

### OOM ladder slot

`OOMScoreAdjust=-800`. Between Camilla's -900 (silence-critical, final
DAC stage) and AEC bridge's -700 (capture-critical, has its own ref
starvation graceful fallback). The fan-in daemon is the upstream source
of the music signal both consume; killing it preferentially over Camilla
makes sense (Camilla can survive a brief input outage by emitting silence;
losing Camilla itself means total silence).

### Memory slice

`Slice=jts-audio.slice`. The slice has `MemorySwapMax=0` (shipped
2026-05-24), so the daemon's pages never get evicted to zram. Audio jitter
from zram decompression latency is the dominant risk on a 1 GB Pi; this
membership shields the work loop from it.

### Failure-mode contract

| Class | Daemon's response | Why |
|---|---|---|
| One input substream silent/idle | Treat as silence; sum continues with remaining inputs | Mux enforces single-source; idle substreams are normal |
| All input substreams silent | Output zeros to maintain ALSA frame timing | Idle state is normal; don't underrun the output |
| One input substream xrun | Log `event=fanin.xrun input=N count=M`; recover via `snd_pcm_recover`; continue | snd_pcm convention |
| Output substream xrun | Log `event=fanin.xrun output count=M`; recover; AEC bridge handles brief ref outage gracefully | The bridge's `claude/aec-bridge-ref-starvation-fix` carries the last ref instead of using silence fallback |
| Unable to open any configured input PCM at startup | Exit 1, let systemd restart with backoff | Every configured input is a private snd-aloop lane that should exist after install; missing one means live topology drift |
| Unable to open output PCM at startup | Exit 1, let systemd restart with backoff | Structural; the dedicated output substream MUST exist |
| Work loop hang | Watchdog ping stops, systemd kills + restarts in ~2 s | The whole point of the heartbeat |
| Repeated wedge (5 restarts in 5 min) | `StartLimitAction=reboot` triggers clean system reboot | Tier 5.1 protection |

### Per-handover ramp

Not implemented in the current mixer. The daemon performs saturating
sample-wise summation, and `jasper-mux` keeps simultaneous-source windows
short by pausing the older renderer on handover. If measured handovers
produce audible clicks, add a small ramp in the mixer with tests and
doctor/state visibility; until then, the extra state machine is deferred.

### Mixer math

**Saturating add (clip at S16_LE bounds).** Matches dmix's existing
behavior, so audio sounds identical to today during the transition. The
alternative (scaled average) is technically more correct but changes
perceived loudness during simultaneous renderers, which mux is supposed
to prevent anyway.

The choice is reversible — if a future case demands scaled summing
(multi-listener environment where simultaneous sources are intentional),
add a configuration flag.

## Resilience + observability contract

Every load-bearing contract is articulated in `docs/HANDOFF-resilience.md`
and the JTS structured-logging conventions. This section confirms how
`jasper-fanin` participates in each. **No new conventions invented; only
existing patterns followed.**

### Systemd unit (`deploy/systemd/jasper-fanin.service`)

```ini
[Unit]
Description=Jasper renderer fan-in (per-substream → summed music reference)
After=sound.target
Requires=sound.target

# StartLimit tuning. Audio-path daemon; transient blip tolerance is
# tight. Tier 5.1 escalates to clean reboot if we hit the burst.
StartLimitIntervalSec=300
StartLimitBurst=5
StartLimitAction=reboot

[Service]
Type=notify
WatchdogSec=30s
TimeoutStopSec=5s          # Load-bearing — 2026-05-11 snd-aloop lesson
Restart=on-failure
RestartSec=5

# OOM ladder slot. See `docs/HANDOFF-resilience.md` Stage 1.
OOMScoreAdjust=-800

# Memory slice (Stage 2 audio protection). Pages never paged to zram.
Slice=jts-audio.slice

# Real-time scheduling. The daemon's main() additionally calls mlockall.
LimitMEMLOCK=infinity
CPUSchedulingPolicy=fifo
CPUSchedulingPriority=30

# Hardening (matches the conventions of other jasper-* units)
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=/var/lib/jasper /run/jasper-fanin

# Config chain
EnvironmentFile=/etc/jasper/jasper.env
EnvironmentFile=-/var/lib/jasper/fanin.env

ExecStart=/opt/jasper/bin/jasper-fanin
ExecReload=/bin/kill -HUP $MAINPID

[Install]
WantedBy=multi-user.target
```

### Structured event logging

All events go to stdout/stderr (captured by journald). Format follows the
project's `event=<subsystem>.<action> [key=value ...]` convention.

| Event | When | Severity |
|---|---|---|
| `event=fanin.boot version=...` | process startup | INFO |
| `event=fanin.config_loaded inputs=N output=... sample_rate=... period_frames=... input_buffer_frames=... output_buffer_frames=...` | parsed runtime config | INFO |
| `event=fanin.input.opened label=airplay pcm=hw:Loopback,1,1 ...` | each input opened | INFO |
| `event=fanin.output.opened pcm=hw:Loopback,0,7 ...` | summed output opened | INFO |
| `event=fanin.mixer.running inputs=N output_xruns=0` | work loop started | INFO |
| `event=fanin.xrun source=input label=airplay count=N` | input overrun recovered | WARN |
| `event=fanin.xrun source=output count=N frames_pending=M` | output underrun recovered | WARN |
| `event=fanin.source_select selected=airplay` | mux selected one input lane (or `selected=auto` / `selected=none`) | INFO |
| `event=fanin.assistant_loudness kind=... final_gain_db=... reason=...` | pre-DSP TTS/cue gain decision | INFO |
| `event=fanin.watchdog.stale age_ms=X` | heartbeat thread skipped a ping (sentinel stale) | WARN |
| `event=fanin.shutdown reason=signal graceful=true` | clean shutdown | INFO |

Why this exact set: the `event=` prefix lets `scripts/jasper-trace.sh`
pick them up alongside other subsystems' events; the verbs match the
`shairport.*` / `wifi_guardian.*` / `aec_bridge.*` patterns documented
elsewhere.

### State and selection control

A UDS socket at `/run/jasper-fanin/control.sock` accepts a single
line command:

- `STATUS` returns the current counters/config snapshot.
- `SELECT <label>` passes only that renderer lane to the sum.
- `AUTO` clears the selected lane and returns to summing active inputs.
- `NONE` passes no renderer lanes. The correction/test lane still
  passes; this is a mux-owned safety primitive, not a user-facing source.

`jasper-mux` is the only production caller for `SELECT` / `AUTO` /
`NONE`. The fan-in daemon does not decide what source should win and
does not know about volume policy; it only executes the cheap audio
gate. It starts in `NONE`, and mux keeps it in `NONE` while no source
has a guarded winner so a renderer that starts between mux polls cannot
leak through at stale volume. Mux prepares the safe volume carrier
before moving the gate. The `correction` lane is always mixed so
room-correction/test sweeps still work while a household source is
manually selected or while the mux has temporarily selected `NONE`.

`STATUS` JSON:

```json
{
  "uptime_seconds": 1234.56,
  "input_buffer_frames": 4096,
  "selection_mode": "select",
  "selected_input": "airplay",
  "inputs": [
    {"label": "spotify", "pcm": "hw:Loopback,1,0", "frames_read": 0, "xrun_count": 0, "catchup_resync_frames": 0, "catchup_events": 0},
    {"label": "airplay", "pcm": "hw:Loopback,1,1", "frames_read": 5928432, "xrun_count": 0, "catchup_resync_frames": 0, "catchup_events": 0},
    {"label": "bluealsa", "pcm": "hw:Loopback,1,2", "frames_read": 0, "xrun_count": 0, "catchup_resync_frames": 0, "catchup_events": 0},
    {"label": "usbsink", "pcm": "hw:Loopback,1,3", "frames_read": 0, "xrun_count": 0, "catchup_resync_frames": 0, "catchup_events": 0},
    {"label": "correction", "pcm": "hw:Loopback,1,4", "frames_read": 0, "xrun_count": 0, "catchup_resync_frames": 0, "catchup_events": 0}
  ],
  "output": {
    "pcm": "hw:Loopback,0,7",
    "sample_rate": 48000,
    "period_frames": 256,
    "buffer_frames": 1024,
    "frames_written": 5928432,
    "xrun_count": 0,
    "snd_pcm_delay_frames": 1024,
    "snd_pcm_delay_ms": 21.333
  },
  "watchdog": {
    "pings_sent": 142,
    "pings_skipped": 0,
    "last_progress_age_ms": 21
  }
}
```

`jasper/control/server.py:_get_state` adds a new top-level `"fanin"` key,
following the same 2 s timeout / fail-soft pattern used for the other
daemons.

### jasper-doctor checks (`jasper/cli/doctor.py`)

Fan-in checks are in the main doctor run-list:

1. **`check_fanin_asound_wiring`** verifies `/etc/asound.conf` has no
   retired `jasper_renderer_*` dmix blocks, defines every private
   renderer/test lane with a pinned 48 kHz stereo S16_LE plug wrapper,
   points `pcm.jasper_capture` at summed substream 7, and keeps
   `pcm.jasper_ref` as the explicit pre-DSP AEC fallback/diagnostic
   wrapper.

2. **`check_fanin_service`** treats disabled or inactive
   `jasper-fanin.service` as a failure, probes
   `/run/jasper-fanin/control.sock`, verifies the live STATUS input
   labels/PCMs and output PCM match the production graph, checks the
   watchdog progress age, and warns if `input_buffer_frames` drops
   below the validated 4096-frame AirPlay burst absorber.

3. **`check_renderer_device_resolvable`** verifies each renderer can
   resolve/open its configured private lane as its runtime systemd user.
   Active lanes may return EBUSY; doctor accepts that only when
   `/proc/asound/.../owner_pid` belongs to the expected renderer unit.

### Persistent state

`/var/lib/jasper/fanin/xrun_history.jsonl` — append-only ring of the last
100 xrun events. Rotated by truncating from the head when the file hits
~10 KB. Each line:

```json
{"ts": "2026-05-25T13:45:21Z", "source": "input", "slot": 1, "frames": 82}
```

Useful for "did the speaker have a bad night?" forensics across reboots.
Survives systemd-managed daemon restarts.

Atomic append: open with `O_APPEND`, write whole line + newline, `fdatasync`.

### Configuration

All knobs as `JASPER_FANIN_*` env vars. Default-OK design: a fresh deploy
works without any wizard interaction.

```sh
# Default values shown — operator override via /etc/jasper/jasper.env
# (or /var/lib/jasper/fanin.env). The Environment= override in the
# systemd unit (deploy/systemd/jasper-fanin.service) sets the
# production input/output buffer overrides below.
JASPER_FANIN_OUTPUT_PCM=hw:Loopback,0,7
JASPER_FANIN_MUSIC_OUTPUT_PCM=                                  # OPTIONAL music-only (pre-TTS) multi-room tap; unset/"disabled" = off (solo). See note below.
JASPER_FANIN_INPUT_PCMS=hw:Loopback,1,0|hw:Loopback,1,1|hw:Loopback,1,2|hw:Loopback,1,3|hw:Loopback,1,4
JASPER_FANIN_INPUT_RENDERERS=spotify|airplay|bluealsa|usbsink|correction   # informational, surfaces in /state
JASPER_FANIN_SAMPLE_RATE=48000
JASPER_FANIN_PERIOD_FRAMES=256                                  # ~5.3 ms at 48k
JASPER_FANIN_INPUT_BUFFER_FRAMES=4096                            # ~85 ms input burst absorber — see "Buffer sizing" below
JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1024                           # ~21 ms output queue toward CamillaDSP/AEC
JASPER_FANIN_TTS_SOCKET=/run/jasper-fanin/tts.sock                # production TTS IPC; "disabled" is rollback/lab only
JASPER_FANIN_TTS_MAX_PENDING_FRAMES=96000                         # 2 s at 48 kHz
JASPER_FANIN_TTS_PROGRAM_DUCK_DB=${JASPER_DUCK_DB:--25}           # override only for lab retuning
JASPER_FANIN_INPUT_RESAMPLER=                                     # DEFAULT-OFF per-input adaptive resampler on the clock-crossing (USB) lane; only "enabled" arms it. See "Per-input resampler" below.
JASPER_FANIN_INPUT_RESAMPLER_LANE=usbsink                         # which lane label the resampler arms on when enabled
JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES=512                    # base ring-fill target for the armed lane (~10.7 ms at 48 k)
JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM=500                   # hard pitch-warp clamp on the host↔DAC rate correction
JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES=2048           # extra held headroom added to TARGET_FRAMES; actual DLL target is target+cushion. See "Per-input resampler" below.
JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES=0                        # input-ring burst headroom; 0 = derive from INPUT_BUFFER_FRAMES, non-zero pins an explicit capacity. Raise to absorb input bursts without adding latency (cuts residual overrun).
```

The list-shaped env vars (`JASPER_FANIN_INPUT_PCMS`,
`JASPER_FANIN_INPUT_RENDERERS`) are **pipe-delimited**, not comma-
delimited. ALSA hw PCM names contain commas (`hw:Loopback,1,0`), so
a comma-delimited shape silently splits one PCM name into three
entries. Discovered via the chunk 2 smoke test; regression-tested
in `config::tests::pipe_delimiter_preserves_commas_inside_hw_pcm_names`.

The TTS socket speaks the same line protocol as outputd's TTS
socket (`GAIN`, `PREPARE_ASSISTANT`, `SEGMENT_START`, `AUDIO`,
`FLUSH_SYNC`, `CLOSE`) plus `PROGRAM_DUCK_ON/OFF`. Fan-in drains those
commands at period boundaries, drops excess queued audio over the
pending-frame budget, applies program ducking only to renderer lanes,
then mixes TTS/cues into the summed buffer before writing toward
CamillaDSP. `PREPARE_ASSISTANT` and profile-bearing `SEGMENT_START`
drive the same content-loudness/profile/peak-cap gain decision used by
outputd; the latest values are exposed under `tts.assistant_loudness` in
the STATUS response alongside `tts.program_duck_active`. Voice's current
fanin ducker is intentionally one-shot — it sends `PROGRAM_DUCK_ON` and
closes, then sends `PROGRAM_DUCK_OFF` from a later connection — so fan-in
does **not** treat TTS socket EOF as duck ownership release. A stuck
program duck is bounded inside the mixer instead: if no TTS audio is
pending and no duck refresh has arrived for the idle TTL, fan-in logs
`event=fanin.program_duck on=false reason=idle_ttl` and releases the
duck. `PROGRAM_DUCK_OFF` is still allowed to release a duck even after an
audio flush advances the TTS epoch; stale `PROGRAM_DUCK_ON` is not
allowed to relatch after a flush.

On an active multiroom bond member, voice bypasses this socket
entirely: the grouping reconciler points it at outputd's TTS server
(`rust/jasper-outputd/src/tts.rs`; the wire vocabulary + parser are the
shared `rust/jasper-tts-protocol` crate both daemons import) so assistant
audio mixes post-round-trip instead of riding the synced stream. One
contract delta to know when comparing acks: both daemons now return a
per-segment playout ledger in the `FLUSH_SYNC` ack (provider item id,
flushed frames, `max_audio_played_ms`, `events[]`) — the ack KEY shape is a
single contract in `jasper-tts-protocol` (`FLUSH_SYNC_ACK_KEYS` /
`FLUSH_SYNC_ACK_EVENT_KEYS`, each daemon guard-tested against it) so the two
renderers cannot drift under the one Python consumer — but the drain point
differs. outputd drains against the real DAC `snd_pcm_delay`, so its
`audio_played_ms` is DAC-true. fan-in sits pre-CamillaDSP and cannot see
the DAC, so its ledger ([`rust/jasper-fanin/src/playout.rs`](../rust/jasper-fanin/src/playout.rs))
counts the mix-commit point — frames popped into the program toward
snd-aloop — which over-reads true playout by the fixed downstream pipeline
depth (the conservative direction for barge-in truncation). See
HANDOFF-multiroom.md Increment 5 PR-2 and
[HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md)
"Robust Barge-In Contract".

**`JASPER_FANIN_MUSIC_OUTPUT_PCM` — the multi-room music-only tap (off by
default).** When set, the mixer writes a SECOND output every period: the
program **post-duck but pre-TTS** — the room's music as played, minus the
assistant. This is the synced stream a grouping leader streams to followers
([`docs/HANDOFF-multiroom.md`](HANDOFF-multiroom.md) §2 "inv-2 realization");
keeping the assistant off it is the inv-3 guarantee (followers never hear the
leader's TTS). The write is a LOSSY side-tap — non-blocking, period-aligned
(`avail_update` gate, so a partial write can't shear a period), drop-on-full —
so it can NEVER back-pressure the primary `JASPER_FANIN_OUTPUT_PCM`, which
stays the sole timing owner (inv-1). A misconfigured/unopenable PCM logs
`event=fanin.music_output.open_failed` and degrades to solo with the primary
path untouched (best-effort open, never fatal). Health is on the STATUS
`music_output` object (`enabled` / `pcm` / `frames_written` / `drops` — a
growing `drops` means the consumer, e.g. snapserver, is behind). Unset / empty
/ `disabled` = no second output, byte-for-byte the pre-multiroom behaviour
(verified by `config::tests::music_output_pcm_off_by_default_and_parses_when_set`
and `mixer::tests::music_only_tap_is_post_duck_and_pre_tts`). **Not yet wired
to snapserver** — that round-trip is Increment 2 of the multi-room build; this
increment is the producer half (and the standalone inv-3 leak fix).

**Period sizing**: 256 frames at 48 kHz = ~5.3 ms wakeup cadence,
frequent enough that the heartbeat sentinel sees real forward
progress every ~5 ms and the Tier 1+2 watchdog catches a wedged
work loop within `WatchdogSec=30s`. No reason to deviate.

#### Input buffer sizing — why 4096, not 1024 (2026-05-26)

The original Phase 2 design defaulted to one shared
`BUFFER_FRAMES=1024`
(~21 ms), reasoned as "half the old dmix; matches the documented
floor for stable dmix-replacement shapes." On 2026-05-26 we found
that floor was too low for the **input** side under the real-world
AirPlay-on-WiFi delivery pattern. The production unit now sets
`JASPER_FANIN_INPUT_BUFFER_FRAMES=4096` (~85 ms). On 2026-06-29 JTS2
testing, the output side was trimmed to
`JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1024` (~21 ms): 1024 held cleanly on
AirPlay while 512 produced immediate output xruns. WiFi burst absorption
therefore stays on the input side instead of becoming downstream
fanin→CamillaDSP/AEC queueing.

As of 2026-06-30, the fan-in output-buffer writer gets its set/unset/floor
decision from [`jasper.audio_runtime_plan`](../jasper/audio_runtime_plan.py)
(`fanin_output_buffer_action`, `resolve_fanin_output_buffer_target`). Temporary
lab frame values belong in `/var/lib/jasper/audio_runtime_overrides.json` via
`jasper-audio-config overrides-set`, with a reason and optional expiry. The
coupling selector is deliberately not a lab override; it still goes through the
ordered `jasper-fanin-coupling-reconcile` transition. The fan-in reconciler
still owns the actual env-file write, daemon restart, and
rollback-on-restart-failure ladder; the plan owns the policy so the doctor,
operator explain CLI, and writer cannot drift. As of the P3/P4 default-flip the
reconciler also has an `--auto` mode (`jasper.fanin.coupling_auto`) that resolves
the SHIPPED default coupling (`shm_ring` on a ring-eligible box, else loopback)
and the USB combo flags on deploy + boot, unless the operator-choice marker
`JASPER_FANIN_COUPLING_CHOICE=operator` freezes the box — see
[HANDOFF-audio-graph-consolidation.md](HANDOFF-audio-graph-consolidation.md).

**The mechanism (kept here for future reference)**:

- 802.11 A-MPDU aggregation batches outgoing AirPlay RTP packets
  into multi-packet radio transmissions. Measured on
  Mac Studio → Pi 5 over a typical home AP: **bursts of ~4 packets
  every ~30 ms** instead of the nominal 1 packet every ~8 ms. The
  largest observed inter-burst gap was ~40 ms.
- shairport faithfully writes whatever it receives into its
  snd-aloop substream. Under WiFi-bursty delivery, the per-substream
  ring fills in ~32 ms bursts and idles between them.
- With buffer = 1024 frames (~21 ms), the ring's headroom is *less*
  than a single inter-burst gap. fanin's continuous read tries to
  drain it during the gap, hits empty, then the writer's next burst
  arrives and overruns the still-not-fully-consumed ring → EPIPE,
  which fanin handles by injecting **one period (5.3 ms) of silence**
  into the mixer output. That's an audible click. **Measured ~1
  xrun every 30-60 s** on real hardware until the buffer was bumped.
- With buffer = 4096 frames (~85 ms), the ring absorbs the
  worst-case ~40 ms gap with ample headroom. **Verified: 0 xruns
  over 4.5 min of AirPlay playback** vs. 44 xruns over the prior
  21 min on the same Mac + WiFi link with `BUFFER_FRAMES=1024`.
- The size matches what the dmix layer (PR #214, replaced by this
  topology) had in its slave's `buffer_size 4096` directive. The
  dmix was **accidentally** also the WiFi-burst absorption layer for
  AirPlay — that's the requirement fanin must continue to satisfy.

**Trade-off**: up to ~85 ms of input-side queue capacity, not a hard
"every frame waits 85 ms" delay. Actual queued audio depends on each
lane's writer/reader fill level, and shairport can observe snd-aloop
delay on its private lane. Net latency is still considerably better
than the dmix topology because fanin's output path (no second dmix
between fanin and CamillaDSP/AEC bridge) is ~85 ms shorter than dmix's
was. The fanin input-side cost is moving the unavoidable burst-
absorption headroom from "before the mixer" (dmix) to "at the mixer's
input ring" (fanin).

**Future tuning**: if a future WiFi setup proves to have tighter
delivery (e.g., a wired AirPlay 2 link, ethernet over a Pi 4/5 with
a USB-Ethernet dongle, or improved router QoS), this is the first
knob to retune. Capture a tcpdump of the actual on-wire
inter-arrival distribution; if max gap is materially below 40 ms,
`JASPER_FANIN_INPUT_BUFFER_FRAMES=2048` (~43 ms) could reduce worst-case
queue capacity by ~43 ms. Treat that as an explicit experiment, not a
cleanup default, and don't tune below `2048` without also confirming
the WiFi gap distribution stays under ~20 ms in your environment.

#### Per-input catch-up resync — free-running lanes (the USB lane)

The work loop reads exactly **one period per lane per iteration** and is
paced by the blocking output write (the local DAC clock). A lane whose
producer is clocked off that *same* DAC — every networked renderer
(AirPlay / Spotify / Bluetooth) and the TTS lane — keeps its capture ring
at ~one period in steady state: it cannot out-produce a consumer that runs
on its own clock. (Its ring can fill *transiently* under a WiFi burst, but
drains back at the DAC rate — that's the buffer-sizing story above.)

The **USB input lane is different.** Its producer is the host (Mac) clock,
not the DAC. The UAC2 gadget's async feedback currently tracks the
snd-aloop jiffies timer (what usbsink consumes), not the DAC, so a small
*residual* rate gap accumulates. With a strict one-period read and no
catch-up, that excess never drains: the lane's snd-aloop ring fills
**monotonically** until it overruns — and by then the *upstream* usbsink
producer queue has already overflowed (`dropped_full`, with `underrun=0`),
because back-pressure never reached the host. The networked lanes never
hit this; only a free-running lane does.

`mixer.rs::drain_input_excess` fixes this with a **bounded per-input
catch-up**. Once per lane per period, before the normal read, it checks
`avail_update()`; if a lane's readable backlog exceeds a high-water
(`CATCHUP_HIGH_WATER_PERIODS = 14` periods ≈ 75 ms at 256-frame periods),
it discards whole periods down to a target (`CATCHUP_TARGET_PERIODS = 1`
period) via a bounded read-and-drop into the lane's existing scratch buffer
(no allocation), capped at `CATCHUP_MAX_DRAIN_PERIODS = 64` reads. The
high-water is chosen to sit **above** the worst-case healthy-lane occupancy
(`HEALTHY_PEAK_OCCUPANCY_PERIODS = 13` — an AirPlay burst stacked on a
stressed-Pi-5 scheduler stall; see "Input buffer sizing" above) so a
healthy lane is never trimmed, and **below** the 16-period input buffer
(`DEFAULT_INPUT_BUFFER_PERIODS`) so the resync fires before an overrun.
Both bounds are pinned by the occupancy-guard test in `mixer.rs`. It is
generic per-input but only ever fires for a lane that actually backs up
monotonically — i.e. the USB lane.

**This is drop-CONTROLLED, not drop-FREE.** A free-running lane loses a
bounded chunk of audio at each resync (an occasional discard at the
residual drift rate), traded against the far worse cascading upstream
overflow it replaces. The drop-free successor — the per-lane adaptive
resampler — now exists behind a flag (next section); the catch-up itself
does **not** resample and stays the default + fallback.

### Per-input adaptive resampler — the drop-FREE successor (DEFAULT-OFF)

`mixer.rs` can instead reconcile the clock-crossing (USB) lane to the DAC
clock with a per-input windowed-sinc resampler (`src/lane_resampler.rs`),
**DLL-steered** to the DAC clock — the drop-free alternative the catch-up's
own docstring defers to. It composes the EXACT shared primitives
`jasper-outputd`'s `content_bridge.rs` uses (`AudioRing` + `SincTable` +
`RateController` from the `jasper-resampler` crate), so the DLL control law
(the spa_dll second-order loop and variance-adaptive bandwidth) is shared, not
reimplemented. The input-lane instance disables the shared controller's
one-period hard-resync because USB burst fill excursions larger than one render
period are valid buffer state, not discontinuities; real discontinuities still
reset the lane on PCM xrun (including xrun reported by `avail_update`) /
explicit idle reset. When armed, the lane holds a small fixed fill
(`JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES` +
`JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES`, daemon defaults 512 + 2048 =
2560 frames ≈ 53 ms; `usb_low_latency_48k` writes 512 + 1536 =
2048 frames ≈ 42.7 ms after the 2026-07-01 jts.local lock test) instead of the 5–75 ms catch-up sawtooth, and the catch-up
drain is bypassed *on that lane only*. Capture-follower sign: a host feeding
faster than the DAC settles to `ratio > 1` (drain), holding the ring at target.
The armed-lane read path drains every frame ALSA reports readable, including
final partial periods, into the resampler's input ring; it re-checks readable
depth after each drained snapshot until empty or the bounded 64-period work cap
is reached. The kernel lane should therefore never fill/overflow in steady
state under the validated callback cadence.

**Cold-start warm-up cushion (`JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES`,
daemon default 2048 = eight periods; `usb_low_latency_48k` route default 1536 =
six periods).** On every (re)lock the resampler PRIMES its ring to
`target + cushion` before producing any output, seats the read cursor at that
deeper fill, and holds that deeper fill as the DLL target. The earlier c57 path
seated deep but then drained the cushion back to `TARGET_FRAMES`; hardware
showed that over-consumption can enter a cold-start limit cycle on the real
bursty USB feed even though steady-input tests pass. Holding the cushion keeps
the jittery first seconds of host arrival above the underfill→silence floor. The
tradeoff is explicit: the cushion adds fixed latency while armed, but avoids a
startup drain transient. The first locked render
period is ramped from silence to de-click the cold zero→audio edge. The held
route cushion is the current conservative DEFAULT-OFF candidate; hardware
must still prove `lock_count=1` / `unlock_count=0` on the bursty USB feed before
it can become production-default behavior. The prime is bounded
(`max_prime_periods` ≈ 1 s of periods): a slow-but-real producer that never
accumulates the full cushion still falls through and locks at whatever safe
depth is buffered, so a real stream can never wedge in prime-silence.

**Burst headroom (`JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES`, default 0 =
derive).** The input ring's capacity is the burst absorber ABOVE the `target`
setpoint — distinct from `TARGET_FRAMES` (which sets latency). `0` derives it
from the lane's `INPUT_BUFFER_FRAMES` (the prior implicit behaviour, floored to
the structural minimum `target + cushion + period + radius + 1`); a non-zero
value pins an explicit capacity. Raise it to absorb input bursts that would
otherwise spike the ring above capacity and drop oldest-first (the residual
`overrun_frames` / usbsink `dropped_full` the on-device counters showed) — more
headroom, no added latency.

**DEFAULT-OFF and inert when off.** Only `JASPER_FANIN_INPUT_RESAMPLER=enabled`
(exact literal) arms it, and only on the lane named
`JASPER_FANIN_INPUT_RESAMPLER_LANE` (default `usbsink`). When off, no
resampler is constructed, the per-lane path is byte-for-byte the
catch-up+strict-read above, and the STATUS shape is unchanged. The catch-up
is intentionally KEPT as the fallback; deleting it is a later,
validation-gated step.

**HIGH-RISK / real-time.** This is a first cut on the audio hot path that
**needs on-device real-time validation** (drop-free under sustained USB play
+ transitions, latency < the catch-up sawtooth, soak, lock stability,
underfill behaviour). Keep it OFF in production until that lands. RT-safety
is designed in (no hot-path allocation, no blocking, bounded per-period
syscalls + interpolation, count-gated logging) but only hardware confirms it.

**Observability**: when armed, the lane's STATUS object gains a nested
`resampler` block — `armed`, `input_frames`, `output_frames`,
`silence_frames`, `overrun_frames`, `ratio_ppm`, `fill_frames`,
`target_fill_frames`, `lock_count`, `unlock_count`. Absent for every unarmed
lane. The engagement-proof gauge is `fill_frames` vs. `target_fill_frames`:
once locked, `fill_frames` sits steady near `target_fill_frames` (the DLL is
holding the ring). `target_fill_frames` is the actual controller target (base
target plus held warm-up cushion), and `ratio_ppm` shows the live pitch-warp the
loop settled on. A growing `unlock_count` is the drop-free analogue of a
catch-up event (the resampler starved and fell back to silence rather than
reading past the buffer); a growing `overrun_frames` means the host outran the
ring. Arming emits a one-time `event=fanin.resampler.armed lane=…
target_frames=… held_target_frames=… warmup_cushion_frames=…
max_adjust_ppm=… ring_frames=…` INFO line. If the
feature is enabled but
`JASPER_FANIN_INPUT_RESAMPLER_LANE` names no live input lane (a typo, or a
non-USB build), NO resampler is constructed and a one-time
`event=fanin.resampler.noop reason=lane_not_found requested=… available=[…]`
WARN names the available labels — so an operator who set the env var but sees
no effect can tell the flag was read but matched nothing (vs. silently
no-op'd). A construction failure logs `event=fanin.resampler.noop
reason=construction_failed …` and falls back to the catch-up drain on that
lane.

**Observability**: each input in the `STATUS` JSON carries
`catchup_resync_frames` (cumulative frames discarded on that lane) and
`catchup_events` (cumulative high-water crossings). Both stay `0` forever
on a DAC-locked lane; a growing pair on `usbsink` is the operator's "this
lane is free-running and we're drop-resyncing it" signal. A rate-limited
`event=fanin.input.catchup` log line (1st event, then every 64th) names
the lane and the discarded/avail/target frames. Neither is ever escalated
— the catch-up keeps the speaker playing, it never restarts a daemon.

## asoundrc changes (`deploy/alsa/asoundrc.jasper`)

### Removed

- `pcm.jasper_renderer_mix` (the dmix block).
- `pcm.jasper_renderer_in` (the plug wrapper).

### Added

Per-renderer/test aliases (the service files use these names; the
underlying `hw:Loopback,0,N` lane remains private to that producer):

```
pcm.librespot_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,0"
        rate 48000
        channels 2
        format S16_LE
    }
}

pcm.shairport_substream  → hw:Loopback,0,1  (same pinned plug shape)
pcm.bluealsa_substream   → hw:Loopback,0,2  (same pinned plug shape)
pcm.usbsink_substream    → hw:Loopback,0,3  (same pinned plug shape)
pcm.correction_substream → hw:Loopback,0,4  (same pinned plug shape)
```

The `plug:` wrapper is what handles each renderer's native rate/format
conversion to 48 kHz S16_LE — same role the old `jasper_renderer_in`
plug played, but per-renderer instead of fronting a shared dmix.

### Changed

- `pcm.jasper_capture` dsnoop's slave shifts from `hw:Loopback,1,0` →
  `hw:Loopback,1,7` (the new "summed music" substream).
- `pcm.jasper_ref` plug wrapper unchanged as explicit fallback/diagnostics
  (slave is still `jasper_capture`).
- CamillaDSP's `v1.yml` capture device unchanged (still `plug:jasper_capture`).

### snd-aloop module parameters

`deploy/modprobe.d/snd-aloop.conf`:

```
options snd-aloop pcm_substreams=8 pcm_notify=0 enable=1 index=6
```

- `pcm_substreams=8` — explicit (default; pinned so future kernel
  updates don't surprise us).
- `pcm_notify=0` — capture side not torn down on parameter changes
  (matters when each renderer owns its own substream and changes rate
  independently).
- `enable=1` — explicit.
- `index=6` — deterministic card number, immune to USB DAC enumeration
  ordering races.

## Renderer service file changes

Each renderer's `--device` / `output_device` flag shifts from
`jasper_renderer_in` to its assigned substream alias:

| Renderer | Old | New |
|---|---|---|
| librespot | `--device jasper_renderer_in` | `--device librespot_substream` |
| shairport-sync | `output_device = "jasper_renderer_in"` | `output_device = "shairport_substream"` |
| bluealsa-aplay | `--pcm=jasper_renderer_in` | `--pcm=bluealsa_substream` |
| jasper-usbsink | `JASPER_USBSINK_PLAYBACK_DEVICE=jasper_renderer_in` | `JASPER_USBSINK_PLAYBACK_DEVICE=usbsink_substream` |

`jasper-doctor`'s `check_renderer_device_resolvable` (the codified post-PR-#223
check) verifies each renderer can open its new device as its runtime
user. Same test as today, new device names.

## Code layout

```
rust/jasper-fanin/                  ← new
  Cargo.toml
  Cargo.lock                        ← pending supply-chain follow-up
  src/
    main.rs                         ← entry, signal handling, sd_notify wiring
    mixer.rs                        ← the work loop: read N → sum → write 1
    state.rs                        ← UDS status server
    config.rs                       ← env-var parsing
    watchdog.rs                     ← progress-sentinel
    xrun_log.rs                     ← /var/lib/jasper/fanin/xrun_history.jsonl

deploy/
  systemd/
    jasper-fanin.service            ← new unit
  modprobe.d/
    snd-aloop.conf                  ← edit module options
  alsa/
    asoundrc.jasper                 ← delete dmix, add per-renderer plugs

jasper/
  cli/
    doctor.py                       ← fan-in wiring/service/renderer checks
  control/
    server.py                       ← add fanin to /state aggregation

scripts/
  aec-probe-latency.sh              ← active AEC delay probe via correction_substream
  aec-probe-pinknoise.sh            ← AEC attenuation probe via correction_substream

tests/
  test_fanin_systemd.py             ← hardware-free pytest for the systemd unit shape
  test_fanin_wiring.py              ← asoundrc + renderer unit topology shape
  test_doctor.py                    ← doctor parser/check behavior
```

The Rust binary is built on the Pi during `install.sh` (taking ~3-5
minutes on a Pi 5). Pre-built binaries are NOT committed to the repo —
matches the project pattern (jasper-aec3 pybind11 binding is also built
on the Pi). This keeps the git tree clean and the build reproducible.

## Why Rust (not Python, C, Go)

| Language | RSS | Realtime safety | Why considered/rejected |
|---|---|---|---|
| **Rust** | ~5 MB | Excellent | Picked. No GIL, no GC, mature `alsa-rs` + `sd-notify` crates, fits the staff-engineer "hot path must be obvious" bar. |
| Python | 25-40 MB (numpy+alsaaudio) | Poor | GIL contention with audio I/O; the 2026-05-11 wedge was a GIL+blocked-I/O failure. Wrong tool for an always-on hot-path daemon. |
| C | ~3 MB | Excellent | Smaller than Rust but: no `cargo`, more painful build integration, sharper foot-guns on memory safety. The marginal RSS win isn't worth the maintenance cost. |
| Go | ~10 MB | Good but GC | GC pauses on the audio path are observable. Less ideal than Rust for the realtime constraint. |

The user's stated goal — "millions of users" of an open-source smart
speaker — argues for the choice that best balances RSS, safety, and
maintainability. Rust wins on all three.

## What this does NOT do

- **Not multi-output.** Single output to substream 7. Future Snapcast /
  second DAC / headphone tap would extend this — likely by adding a
  second output substream that mirrors substream 7's content. ADR-noted
  for future revisit; not built in v1.
- **Not a policy router.** It has one intentionally dumb selected-input
  gate for mux-controlled source override. It does not choose winners,
  inspect renderer state, or route to multiple outputs.
- **Not a DSP stage.** No EQ, no resampling, no room correction.
  CamillaDSP owns all that downstream.
- **Not aware of source state.** It knows only a selected input label
  or auto/null. Mux owns "current primary", renderer probing,
  source-specific preemption APIs, and user source selection. Current
  examples: AirPlay loses via shairport-sync MPRIS `Stop`, Spotify via
  Web API pause or librespot restart fallback, and USB sink via its
  local silence endpoint.
- **Not PipeWire.** Per the AGENTS.md "architecture is fixed; swap the
  engine, not the topology" rule (scoped to AEC but spirit applies to
  the bus): this is the smallest viable shape, not a bus rewrite.

## Retired topology switch

The old `deploy/bin/jasper-audio-topology` CLI and
`/var/lib/jasper/audio_topology.env` state file were removed when
fan-in became the only supported renderer topology. Keeping both
paths created a single-source-of-truth violation: deploy could render
a dmix-era `/etc/asound.conf` while the persisted state and renderer
units still said fanin. That exact mixed state makes
`pcm.jasper_capture` point at substream 0 (now a private input lane)
instead of substream 7 (the summed output), starving the AEC bridge's
reference signal.

Current deploy behavior:

- `deploy/alsa/asoundrc.jasper` is the fan-in asoundrc.
- renderer units point directly at their private lanes
  (`librespot_substream`, `shairport_substream`,
  `bluealsa_substream`, `usbsink_substream`).
- `install.sh` enables `jasper-fanin.service` directly.
- `install.sh` archives/removes stale
  `/var/lib/jasper/audio_topology.env` and removes any installed
  `/usr/local/sbin/jasper-audio-topology`.

Current doctor behavior:

- `check_fanin_asound_wiring` verifies no legacy
  `jasper_renderer_*` blocks exist, all private renderer lanes are
  present, and `pcm.jasper_capture` dsnoops `hw:Loopback,1,7`.
- `check_fanin_service` treats disabled/inactive `jasper-fanin` as a
  failure, probes the UDS STATUS endpoint, and warns if runtime
  `input_buffer_frames` drops below 4096.
- `check_renderer_device_resolvable` treats EBUSY on an active
  private lane as OK only when the lane owner is the expected renderer
  unit; Unknown PCM is always a real failure.

## Historical Migration Plan

The phase plan below is preserved as decision archaeology. It is no
longer the operational runbook: Phase 4 has effectively shipped, and
the dmix path has been removed rather than kept behind a rollback flag.

Phase ordering matters because each phase needs the previous one's
infrastructure to be in place.

### Phase 1 — modprobe + asoundrc shape (no daemon yet)

- Pin snd-aloop module params (no behavior change today; future-proofs).
- Add the per-renderer substream alias PCMs to asoundrc (defined but
  not yet used by renderers).
- The dmix block stays. Nothing breaks.

**Verify:** `aplay -L | grep substream` shows the new aliases.
**Soak:** zero — pure config addition, no traffic.

### Phase 2 — the daemon (built, installed, but inactive)

- Land the Rust source tree + Cargo.toml. Cargo.lock is still pending
  supply-chain follow-up; see
  [HANDOFF-supply-chain.md](HANDOFF-supply-chain.md).
- `install.sh` builds it and installs to `/opt/jasper/bin/jasper-fanin`.
- Land the systemd unit, but with `WantedBy=` set to a non-existent
  target so it doesn't activate yet. Or land disabled with
  `systemctl is-enabled` showing `disabled`.

**Verify:** `cargo build --release` succeeds on Pi; `jasper-fanin --help`
runs; `/opt/jasper/bin/jasper-fanin` exists; service unit is loadable
but inactive.
**Soak:** zero — daemon doesn't touch audio.

### Phase 3 — opt-in activation (daemon runs in parallel with dmix)

- Add a feature flag: `JASPER_AUDIO_TOPOLOGY=fanin` in
  `/etc/jasper/jasper.env`. Default `dmix` (current). (This flag is
  retired — Phase 4 below deleted the dmix path and the flag along
  with it; `jasper/cli/doctor/audio.py` calls it "the retired
  dmix/fanin switcher" and `env-migrations.sh`'s
  `retire_audio_topology_switch()` strips any leftover state file on
  every install. Kept here only as build-history narrative.)
- When `=fanin`: jasper-fanin starts; renderers point at per-renderer
  substreams; `pcm.jasper_capture` dsnoop targets substream 7.
- When `=dmix`: existing topology, unchanged.
- Operator can flip the flag, restart audio chain, and observe.

**Verify:** music plays under both topologies; AEC works under both;
no xruns over a 1-hour active listening session.
**Soak:** **72 hours minimum on the `=fanin` flag** under mixed load
(Spotify, AirPlay, BT, TTS, voice). Acceptance criteria below.

### Phase 4 — default-on (superseded by production cleanup)

- Flip the default to `fanin`.
- Original conservative plan: after 30 days of clean operation, delete
  the dmix path entirely.
- Actual 2026-05-26 follow-up: deleted the dmix path immediately after
  measured AirPlay validation and replaced the rollback flag with
  jasper-doctor drift checks, because keeping two paths caused the
  `/etc/asound.conf` split-brain that starved the AEC bridge reference.

**Verify:** fresh install via `bash deploy/install.sh` lands on the
fanin topology with zero operator interaction.
**Soak:** captured by the 30-day continuous-use window.

## Historical Acceptance Criteria

- [x] AirPlay latency-offset math accounts for CamillaDSP target fill,
  fan-in output, and output dmix only: expected fixed downstream
  compensation is now ~171 ms rather than the old renderer-dmix-era
  ~192 ms. External end-to-end loopback latency was not
  remeasured during the 2026-05-26 cleanup.
- [x] shairport "Dropping out of date packet" rate: 0 over the
  measured post-cutover AirPlay validation window.
- [x] CamillaDSP `PB: Prepare playback after buffer underrun` rate: 0.
- [ ] AEC ERLE measured against a known echo signal stays within 1 dB
  of the pre-PR baseline.
- [ ] Bluealsa codec switch (SBC ↔ AAC) at the substream boundary: no
  audible glitch beyond the existing baseline.
- [x] `event=fanin.xrun` count: 0 under steady AirPlay listening after
  the 4096-frame buffer cutover. Mixed-source stress remains a useful
  future check.
- [ ] jasper-fanin RSS <8 MB over the soak.
- [ ] CPU <2% of one Pi 5 core at steady state, <5% under handover.
- [x] Watchdog never fires unprovoked during post-deploy validation
  (no `event=fanin.watchdog.stale` lines under realistic load).

These were the original promotion checks. The unchecked rows are still
useful future soak targets for Bluetooth/Spotify/ERLE measurement; they
are not blockers for the 2026-05-26 production cutover because keeping
the old dmix path created a worse split-brain failure mode.

If xruns appear under a future WiFi-storm stress test at
`JASPER_FANIN_INPUT_BUFFER_FRAMES=4096`: escalate to PREEMPT_RT kernel,
re-soak, and document it as a hard requirement. Otherwise stock kernel
remains the supported floor.

## Resolved Implementation Choices

1. **ALSA binding.** The daemon uses `alsa-rs`, which is the right
   level of abstraction for direct PCM open/read/write and hardware
   parameter control.
2. **Per-input ring-buffer sizing.** Production default is 4096 frames
   after real AirPlay/WiFi validation. Doctor warns below that value.
3. **Output substream lifecycle on daemon restart.** systemd restarts
   `jasper-fanin.service`; CamillaDSP and the AEC bridge see a brief
   zero/silence gap while the output PCM is re-opened. That is inside
   the existing resilience contract.
4. **Reload semantics.** Runtime retuning is done with a service
   restart. Keeping the daemon small and explicit won over a HUP reload
   path that would need to preserve output PCM state across config
   mutation.

## What's NOT in this design

These are deliberate omissions to keep v1 small. Each is a known
follow-on if/when warranted.

- **Per-input metering published continuously.** Today's design exposes
  RMS only via the on-demand UDS STATUS query. A streaming metrics
  endpoint (Prometheus-style or pub-sub) is deferred — it's not in the
  rest of the project either, and the on-demand query is enough for
  jasper-doctor's "is anyone making sound" check.
- **A `/fanin/` web wizard.** No UI surface needed for v1 — the daemon
  is auto-configured. Wizard surface is a follow-on if an operator
  needs to disable a specific input (e.g., "disable USB input").
- **Sample-accurate timestamps on the output substream.** ALSA's
  default jiffies-driven timer is sufficient. The PCM clock on the
  output substream is the same as the loopback's, which is the same
  as today's path.
- **General native-rate conversion between inputs and output.** All inputs are
  still declared at 48 kHz S16_LE (the substream rate); each renderer's plug
  wrapper handles its own native-rate conversion before reaching the substream.
  The optional default-off USB-lane resampler above is clock reconciliation for
  the one foreign-clock input, not a general sample-rate conversion layer.

## References

- [`docs/HANDOFF-resilience.md`](HANDOFF-resilience.md) — the multi-tier
  resilience ladder this design composes with.
- [`docs/audio-paths.md`](audio-paths.md#adding-a-new-music-source) —
  the canonical checklist for adding another music source to the
  fan-in topology.
- [`docs/HANDOFF-airplay.md`](HANDOFF-airplay.md) — Pattern A3 (the
  current Tier 1A fix) and the underlying snd_pcm_delay() limitation
  this Tier 2A work avoids by deleting the dmix.
- [`docs/HANDOFF-aec.md`](HANDOFF-aec.md) — the AEC reference tap that
  shifts substream pairs in Tier 2A.
- [`docs/HANDOFF-tier5-watchdog-liveness.md`](HANDOFF-tier5-watchdog-liveness.md) —
  the userspace-liveness probing that Tier 5.2 added; the fan-in
  daemon participates implicitly via its `/healthz` analog.
- [`jasper/watchdog.py`](../jasper/watchdog.py) — the Python progress-
  sentinel reference implementation; Rust mirror lives in
  `rust/jasper-fanin/src/watchdog.rs`.
- [PR #214](https://github.com/jaspercurry/JTS/pull/214) — the dmix
  introduction this design unwinds.
- [PR #308](https://github.com/jaspercurry/JTS/pull/308) — the parent
  PR carrying this work alongside Tiers 1A and the target_level trim.
- [Linux `sound/drivers/aloop.c`](https://github.com/torvalds/linux/blob/master/sound/drivers/aloop.c)
  — the kernel source confirming per-substream cable independence.
- [ALSA Project Matrix:Module-aloop](https://www.alsa-project.org/wiki/Matrix:Module-aloop)
  — the documentation of the 8-substream-per-card model.
- OSPERT 2024, Dewit et al. — "A Preliminary Assessment of the real-time
  capabilities of the Raspberry Pi 5" — the scheduling-latency numbers
  driving the SCHED_FIFO + PREEMPT_RT-gated design.

Last verified: 2026-07-01 (`usb_low_latency_48k` now route-owns the USB input
resampler: target 512 + cushion 1536, held target 2048, ring 4096, max adjust
500 ppm. jts.local tuning found the global 4096-frame fan-in input buffer still
required: 512/1024 never locked, 2048 and 3072 lock-churned and generated
silence. A clean 5-minute steady-state sample with Rust bridge 256/3, fan-in
4096/1024, CamillaDSP 256/1536, and outputd 128/256 had zero new bridge xruns,
zero bridge underflows, zero resampler relocks, and zero resampler silence.
Route-latency click/capture evidence remains required before claiming p95/p99.)
