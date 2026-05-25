# Handoff: Tier 2A fan-in daemon — design

> **Status: shipped (Phase 1 + Phase 2 chunks 1-4 landed in PR #308,
> 2026-05-25). Disabled by default.** The daemon source, the systemd
> unit, the install.sh build wiring, the jasper-doctor checks, and the
> `/state` aggregation are all in place. Activating the topology (Phase
> 3) still requires explicit operator opt-in — set
> `JASPER_AUDIO_TOPOLOGY=fanin` and `systemctl enable --now
> jasper-fanin.service`, then complete the 72-hour soak before Phase 4
> (default-on). The "Status" line moves to operational once Phase 4
> lands.

## TL;DR

JTS's current music topology has all renderers (librespot, shairport-sync,
bluealsa-aplay, future HDMI) writing through a userspace dmix
(`pcm.jasper_renderer_mix`, added 2026-05-22 by PR #214) into a single
snd-aloop substream pair. The dmix adds ~85 ms of buffering that's
structurally invisible to `snd_pcm_delay()`, costs ALSA-config complexity,
and complicates the AEC reference tap. The current Tier 1A fix
([PR #308](https://github.com/jaspercurry/JTS/pull/308)) compensates the
invisible delay; Tier 2A **deletes the dmix layer entirely** by exploiting
snd-aloop's per-substream rate independence.

Each renderer gets its own snd-aloop substream (`hw:Loopback,0,0..3`). A
small Rust daemon (`jasper-fanin`) reads the capture side of each
substream, sums them sample-wise, and writes to a single dedicated
"summed music" substream (`hw:Loopback,0,7`). CamillaDSP and the AEC bridge
both dsnoop on the capture side of that summed substream (`hw:Loopback,1,7`)
— same shape as today, just one substream pair shifted. The dmix layer
and its ~85 ms of latency disappear; the AEC reference signal becomes
cleaner (post-mix, single point of truth); adding a future HDMI source is
"assign substream 4" rather than "fight contention."

Net latency saving over the current Tier 1A topology: ~85 ms (the
renderer-side dmix buffer). Combined with the Tier 1A latency offset fix
and the Tier 1B-equivalent CamillaDSP `target_level` trim that landed in
PR #308, the total round-trip from "shairport accepts RTP packet" to
"speaker emits sound" drops from the current ~150 ms to ~65 ms. The
saving survives any future PipeWire migration.

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
  (reserved)           → hw:Loopback,0,4    [for HDMI input or future renderers]
  (reserved)           → hw:Loopback,0,5    [debug/monitor mirror, for offline AEC capture]
  (reserved)           → hw:Loopback,0,6
                          
jasper-fanin (the new Rust daemon):
  reads from           ← hw:Loopback,1,0..3 (via per-substream dsnoop or direct hw)
  sums sample-wise
  writes to            → hw:Loopback,0,7

The "summed music" substream:
  CamillaDSP captures  ← pcm.jasper_capture → dsnoop on hw:Loopback,1,7
  AEC bridge captures  ← pcm.jasper_ref     → dsnoop on hw:Loopback,1,7
                                                  (same substream, both consumers)

CamillaDSP → jasper_out dmix (TTS sums in here) → Apple USB-C dongle
```

### What this preserves

- **TTS bypass of CamillaDSP.** Unchanged. TTS still writes to the
  `jasper_out` dmix on the dongle; music still goes through CamillaDSP
  first; the ducker's `main_volume` still attenuates only music.
  `TtsVolumeTracker` continues to observe `playback_rms` to scale TTS.
- **AEC reference tap shape.** Unchanged at the consumer end —
  `pcm.jasper_ref` is still a plug-wrapped dsnoop; the AEC bridge code
  doesn't change at all. Only the underlying substream-pair shifts from
  `(1,0)` to `(1,7)`.
- **Mux arbitration.** Unchanged. `jasper-mux` still does latest-source-wins
  via MPRIS/Web-API pause. The substream-per-renderer assignment recovers
  the implicit `-EBUSY` floor as a defense-in-depth backstop.
- **Renderer service files.** Each renderer's `--device` flag changes
  from `jasper_renderer_in` (the plug-on-dmix) to its assigned substream
  alias (`pcm.librespot_substream`, etc.). The renderer code is unchanged.
- **jasper_out dongle dmix.** Unchanged. Still where music + TTS sum
  before the DAC.
- **CamillaDSP config (v1.yml).** Capture device stays `plug:jasper_capture`.
  The dsnoop's underlying substream shifts from `(1,0)` to `(1,7)` in the
  asoundrc — invisible to CamillaDSP itself.

### What this deletes

- `pcm.jasper_renderer_mix` dmix block in asoundrc — gone.
- `pcm.jasper_renderer_in` plug wrapper — gone.
- The 85 ms invisible-to-shairport buffer.
- The need for shairport's `audio_backend_latency_offset_in_seconds` to
  carry a dmix term — the derivation in `jasper-apply-airplay-mode`
  already falls back gracefully when no `pcm.jasper_renderer_mix` block
  exists, so it auto-reverts to compensating only CamillaDSP's
  `target_level` (i.e., the offset drops from `-0.106667` to `-0.021333`
  after this change, a further ~85 ms saving on AirPlay scheduling).

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
| Unable to open any input PCM at startup | Continue with the inputs that opened; structural failure of one renderer is not fatal | Renderers may not be enabled (Bluetooth off, USB-in off) |
| Unable to open output PCM at startup | Exit 1, let systemd restart with backoff | Structural; the dedicated output substream MUST exist |
| Work loop hang | Watchdog ping stops, systemd kills + restarts in ~2 s | The whole point of the heartbeat |
| Repeated wedge (5 restarts in 5 min) | `StartLimitAction=reboot` triggers clean system reboot | Tier 5.1 protection |

### Per-handover ramp

When the "active primary" input changes (e.g., AirPlay handover to Spotify),
a hard step-discontinuity at the sample boundary clicks audibly. The
daemon applies a 10 ms cosine ramp on the transition:

```
during handover frame:
    ramp_factor = 0.5 * (1 - cos(pi * elapsed_ms / 10))
    output = old_input * (1 - ramp_factor) + new_input * ramp_factor
```

Cost: +480 samples of handover latency (~10 ms), zero steady-state cost.
Adds ~30 lines of Rust on top of the main mixer loop.

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
| `event=fanin.started inputs=N output=hw:Loopback,0,7 rate=48000 period=256` | startup ready | INFO |
| `event=fanin.input.opened slot=N pcm=hw:Loopback,1,N` | each input opened (lazy, per-renderer) | INFO |
| `event=fanin.input.silent slot=N renderer=spotify duration_ms=420` | input went silent for ≥250 ms | INFO |
| `event=fanin.input.active slot=N renderer=spotify` | input transitioned silent→active | INFO |
| `event=fanin.handover from=spotify to=airplay ramp_ms=10` | active primary changed | INFO |
| `event=fanin.xrun source=input slot=N frames=M` | snd_pcm_recover triggered | WARN |
| `event=fanin.xrun source=output frames=M` | output xrun | WARN |
| `event=fanin.input.frame_late slot=N lag_ms=X` | input read returned later than expected by X ms | WARN (>5 ms) |
| `event=fanin.watchdog.stale age_ms=X` | heartbeat thread skipped a ping (sentinel stale) | WARN |
| `event=fanin.fatal reason=... detail=...` | structural failure leading to exit | ERROR |
| `event=fanin.shutdown reason=sigterm graceful=true` | clean shutdown | INFO |

Why this exact set: the `event=` prefix lets `scripts/jasper-trace.sh`
pick them up alongside other subsystems' events; the verbs match the
`shairport.*` / `wifi_guardian.*` / `aec_bridge.*` patterns documented
elsewhere.

### State exposure via `/state`

A UDS socket at `/run/jasper-fanin/control.sock` accepts a single
command, `STATUS`, returning JSON:

```json
{
  "running": true,
  "uptime_seconds": 1234.5,
  "inputs": [
    {"slot": 0, "pcm": "hw:Loopback,1,0", "renderer": "spotify", "active": false, "frames_read": 0, "xrun_count": 0, "rms_dbfs": null},
    {"slot": 1, "pcm": "hw:Loopback,1,1", "renderer": "airplay", "active": true,  "frames_read": 5928432, "xrun_count": 0, "rms_dbfs": -22.4},
    {"slot": 2, "pcm": "hw:Loopback,1,2", "renderer": "bluealsa", "active": false, "frames_read": 0, "xrun_count": 0, "rms_dbfs": null},
    {"slot": 3, "pcm": "hw:Loopback,1,3", "renderer": "usbsink", "active": false, "frames_read": 0, "xrun_count": 0, "rms_dbfs": null}
  ],
  "output": {
    "pcm": "hw:Loopback,0,7",
    "sample_rate": 48000,
    "frames_written": 5928432,
    "xrun_count": 0
  },
  "current_primary": "airplay",
  "handover_count": 3,
  "last_handover_at": "2026-05-25T13:45:21Z",
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

Two checks, added to the run-list:

1. **`check_fanin_running`**:
   - `systemctl is-active jasper-fanin.service` == "active"
   - UDS probe to `/run/jasper-fanin/control.sock` returns valid JSON
   - `watchdog.last_progress_age_ms < 1000` (catches "service active but
     work loop wedged")
   - Returns OK with summary; WARN if work loop hasn't ticked in >1 s.

2. **`check_fanin_output_substream`**:
   - Reads `/proc/asound/Loopback/pcm0p/sub7/status`
   - Expects `state: RUNNING` and the writer's PID matches `jasper-fanin`.
   - WARN if not running, FAIL if another process owns the substream.

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
JASPER_FANIN_OUTPUT_PCM=hw:Loopback,0,7
JASPER_FANIN_INPUT_PCMS=hw:Loopback,1,0|hw:Loopback,1,1|hw:Loopback,1,2|hw:Loopback,1,3
JASPER_FANIN_INPUT_RENDERERS=spotify|airplay|bluealsa|usbsink   # informational, surfaces in /state
JASPER_FANIN_SAMPLE_RATE=48000
JASPER_FANIN_PERIOD_FRAMES=256                                  # ~5.3 ms at 48k
JASPER_FANIN_BUFFER_FRAMES=1024                                 # ~21 ms — well below current dmix
JASPER_FANIN_HANDOVER_RAMP_MS=10
JASPER_FANIN_SILENCE_THRESHOLD_DBFS=-90
```

The list-shaped env vars (`JASPER_FANIN_INPUT_PCMS`,
`JASPER_FANIN_INPUT_RENDERERS`) are **pipe-delimited**, not comma-
delimited. ALSA hw PCM names contain commas (`hw:Loopback,1,0`), so
a comma-delimited shape silently splits one PCM name into three
entries. Discovered via the chunk 2 smoke test; regression-tested
in `config::tests::pipe_delimiter_preserves_commas_inside_hw_pcm_names`.

Reasoning on the period/buffer: 1024-frame buffer (~21 ms) is half the
old dmix's 4096; matches the documented floor for stable dmix-replacement
shapes. Period 256 keeps wakeup cadence frequent enough that the
heartbeat sentinel sees real forward progress every ~5 ms.

## asoundrc changes (`deploy/alsa/asoundrc.jasper`)

### Removed

- `pcm.jasper_renderer_mix` (the dmix block).
- `pcm.jasper_renderer_in` (the plug wrapper).

### Added

Per-renderer aliases (informational; renderers still use `hw:Loopback,0,N`
directly in their service flags, but the aliases document the intent and
provide a future migration surface):

```
pcm.librespot_substream  { type plug; slave.pcm "hw:Loopback,0,0"; }
pcm.shairport_substream  { type plug; slave.pcm "hw:Loopback,0,1"; }
pcm.bluealsa_substream   { type plug; slave.pcm "hw:Loopback,0,2"; }
pcm.usbsink_substream    { type plug; slave.pcm "hw:Loopback,0,3"; }
```

The `plug:` wrapper is what handles each renderer's native rate/format
conversion to 48 kHz S16_LE — same role the old `jasper_renderer_in`
plug played, but per-renderer instead of fronting a shared dmix.

### Changed

- `pcm.jasper_capture` dsnoop's slave shifts from `hw:Loopback,1,0` →
  `hw:Loopback,1,7` (the new "summed music" substream).
- `pcm.jasper_ref` plug wrapper unchanged (slave is still `jasper_capture`).
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
  Cargo.lock                        ← committed (deterministic builds)
  src/
    main.rs                         ← entry, signal handling, sd_notify wiring
    mixer.rs                        ← the work loop: read N → sum → write 1
    state.rs                        ← UDS status server
    config.rs                       ← env-var parsing
    watchdog.rs                     ← progress-sentinel
    handover.rs                     ← cosine ramp
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
    doctor.py                       ← add 2 checks
  control/
    server.py                       ← add fanin to /state aggregation

scripts/
  build-jasper-fanin.sh             ← `cargo build --release`, copy to /opt/jasper/bin

tests/
  test_fanin_unit.py                ← hardware-free pytest for the systemd unit shape
  test_fanin_doctor.py              ← hardware-free pytest for the doctor checks
  test_asoundrc_topology.py         ← assert renderer substream aliases exist, dmix block removed
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
- **Not a router.** No "send this input to this output" logic. Just sum
  all inputs and emit one stream.
- **Not a DSP stage.** No EQ, no resampling, no room correction.
  CamillaDSP owns all that downstream.
- **Not aware of mux state.** Doesn't know which renderer is "the
  current primary" — it sums whatever's active. Mux's "latest source
  wins" still works because at most one renderer should be producing
  signal at a time. The fan-in daemon is dumb summation; the
  intelligence lives in mux.
- **Not PipeWire.** Per the AGENTS.md "architecture is fixed; swap the
  engine, not the topology" rule (scoped to AEC but spirit applies to
  the bus): this is the smallest viable shape, not a bus rewrite.

## Switching between topologies (Phase 3 testing)

`deploy/bin/jasper-audio-topology` (installed at
`/usr/local/sbin/jasper-audio-topology`) is the CLI that flips the
chain between `dmix` and `fanin` atomically. It:

- writes `/var/lib/jasper/audio_topology.env` (the single source of
  truth for the active mode and the per-renderer device env vars)
- swaps `/etc/asound.conf` between the dmix and fanin variants
  (backing up the dmix version on first switch)
- regenerates `/etc/shairport-sync.conf` via the existing
  `jasper-apply-airplay-mode` (which now reads
  `JASPER_AUDIO_TOPOLOGY`)
- runs `systemctl daemon-reload` so each renderer's
  `EnvironmentFile=-/var/lib/jasper/audio_topology.env` re-resolves
- enables+starts `jasper-fanin.service` (fanin) or stops+disables
  it (dmix)
- restarts the renderer + DSP chain in dependency order
- post-restart, verifies critical daemons came up

Usage:

```sh
sudo jasper-audio-topology               # show status
sudo jasper-audio-topology status        # same
sudo jasper-audio-topology fanin         # switch to fanin
sudo jasper-audio-topology dmix          # switch back
sudo jasper-audio-topology fanin --dry-run   # preview without flipping
```

A jasper-doctor check (`check_audio_topology_state`) catches the
"half-switched" failure mode where the env file declares one mode
but the daemons disagree (e.g., env says `fanin` but
`jasper-fanin.service` isn't running — that means the renderers
are writing to per-renderer substreams with nobody reading them,
i.e. the speaker is silent).

The CLI is the migration tool for Phase 3 (operator opt-in
testing). When Phase 4 ships default-on, this script becomes the
"emergency revert to dmix" tool — still useful, narrower scope.

## Migration plan

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

- Land the Rust source tree + Cargo.toml + Cargo.lock.
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
  `/etc/jasper/jasper.env`. Default `dmix` (current).
- When `=fanin`: jasper-fanin starts; renderers point at per-renderer
  substreams; `pcm.jasper_capture` dsnoop targets substream 7.
- When `=dmix`: existing topology, unchanged.
- Operator can flip the flag, restart audio chain, and observe.

**Verify:** music plays under both topologies; AEC works under both;
no xruns over a 1-hour active listening session.
**Soak:** **72 hours minimum on the `=fanin` flag** under mixed load
(Spotify, AirPlay, BT, TTS, voice). Acceptance criteria below.

### Phase 4 — default-on (dmix path retired)

- Flip the default to `fanin`.
- After 30 days of clean operation, delete the dmix path entirely
  (asoundrc block + the latency-offset derivation's dmix term + the
  Tier 2 mux escalation that compensates for missing EBUSY).

**Verify:** fresh install via `bash deploy/install.sh` lands on the
fanin topology with zero operator interaction.
**Soak:** captured by the 30-day continuous-use window.

## Acceptance criteria for Phase 3 → Phase 4 promotion

- [ ] End-to-end music latency drops by ≥75 ms vs the current
  (post-PR-#308) baseline (~150 ms → ~75 ms).
- [ ] shairport "Dropping out of date packet" rate: 0 over the soak
  window.
- [ ] CamillaDSP `PB: Prepare playback after buffer underrun` rate: 0.
- [ ] AEC ERLE measured against a known echo signal stays within 1 dB
  of the pre-PR baseline.
- [ ] Bluealsa codec switch (SBC ↔ AAC) at the substream boundary: no
  audible glitch beyond the existing baseline.
- [ ] `event=fanin.xrun` count: ≤2 per hour under stress; 0 under
  steady listening.
- [ ] jasper-fanin RSS <8 MB over the soak.
- [ ] CPU <2% of one Pi 5 core at steady state, <5% under handover.
- [ ] Watchdog never fires unprovoked (no `event=fanin.watchdog.stale`
  lines under realistic load).

If xruns appear under WiFi-storm stress test: escalate to PREEMPT_RT
kernel, re-soak, document as a hard requirement. Otherwise stock kernel
is the supported floor.

## Open design questions

These need answers during Phase 2 build, not before:

1. **`alsa-rs` vs `cpal` vs raw bindgen.** Default to `alsa-rs` — mature,
   idiomatic, the right level of abstraction. Confidence: high.
2. **Per-input ring-buffer sizing.** Each input needs a small jitter
   buffer because renderers won't deliver in lockstep. 2× period (~10 ms)
   is the default; bench-measure during Phase 2.
3. **Output substream lifecycle on daemon restart.** If `jasper-fanin`
   crashes and systemd takes ~2 s to restart it, the output substream
   closes and reopens. CamillaDSP's dsnoop on the capture side sees a
   brief stream of silence. The AEC bridge sees the same. Acceptable
   under the resilience contract (Tier 1+2 catches it), but worth
   measuring: how long is the brief silence? If it's <100 ms it's a
   non-issue; if longer, the daemon may need to keep the output PCM
   open in a "muted/zero-fill" mode during shutdown.
4. **HUP-reload semantics.** `ExecReload=/bin/kill -HUP $MAINPID` —
   the daemon should re-read its env-file config on HUP without
   dropping the output substream. Implementation detail; not load-bearing
   for v1.

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
- **Resampling between inputs and output.** All inputs declared at
  48 kHz S16_LE (the substream rate); each renderer's plug wrapper
  handles its own native-rate conversion before reaching the substream.

## References

- [`docs/HANDOFF-resilience.md`](HANDOFF-resilience.md) — the multi-tier
  resilience ladder this design composes with.
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

Last verified: 2026-05-25.
