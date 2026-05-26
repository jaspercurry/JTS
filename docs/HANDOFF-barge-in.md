# Robust barge-in — design space + costing record

**Status (2026-05-23):** Research only. No implementation in flight.
This document captures the option space for upgrading JTS's
barge-in story from "Silero VAD threshold filtering" (today) to
"AEC cancellation of TTS in the mic" (canonical).

> ### ⚠️ Read this before reading the options below
>
> [`AGENTS.md`](../AGENTS.md) and
> [`CONTRIBUTING.md`](../CONTRIBUTING.md) (both updated 2026-05-23)
> establish a standing rule: **for the AEC subsystem,
> architectural changes are not reviewable; engine swaps and
> tuning are.** The named-rejected paths include "PipeWire
> `module-echo-cancel`," "replacing snd-aloop with PipeWire
> fanout," "dual-USB-sink hardware-AEC retry," and "custom XVF
> firmware." Targeted single-knob OS-layer fixes (a specific
> ALSA setting, a kernel module parameter) ARE acceptable when
> measurement has localized the root cause to that layer.
>
> Options A and B below are **explicitly the kind of speculative
> re-architecture that policy rejects.** This doc records the
> reasoning so a future contributor (or future Claude session)
> doesn't re-derive the costing from scratch and propose what's
> already been declined. It is **not** a menu of live options.
>
> The operative recommendation is **Option C** — measure
> first, then if measurement justifies it, address with
> engine-internal tuning inside `jasper/cli/aec_bridge.py` or
> the `jasper_aec3` binding. That path stays inside the policy.
>
> If barge-in measurement ever produces a case strong enough to
> reconsider the policy, the costing in Options A/B is here.
> Reopening that conversation is a deliberate move, not an
> agent-side proposal.

The trigger: a future feature request — make barge-in work
reliably under loud music. The current implementation works for
"quiet room, raised voice" but has known limits (see
[Today's barge-in: what works](#todays-barge-in-what-works-and-what-doesnt)).
The naive fix ("put TTS in the AEC reference") collides with
the audio architecture in non-obvious ways. This doc explains
why, costs the architectural options for the historical record,
and recommends the measurement path that stays inside policy.

---

## Today's barge-in: what works and what doesn't

JTS supports barge-in today via **local Silero VAD gating**, not
AEC. Mechanism:

- During a turn, [`WakeLoop._handle_session_frame`](../jasper/voice_daemon.py)
  runs every mic frame through Silero VAD ([`SpeechVAD`](../jasper/voice_daemon.py)).
- A frame is forwarded to the realtime LLM **only if** the VAD
  score exceeds `END_OF_UTTERANCE_SPEECH_THRESHOLD = 0.15`.
- TTS bleed through the mic typically scores below that
  threshold (the comment block at
  [`voice_daemon.py:315-327`](../jasper/voice_daemon.py) records
  the calibration: TTS bottoms out ~0.13, real soft speech ~0.19,
  music vocals ~0.13 — 0.15 sits between them).
- When the gate fires, [`tts.flush()`](../jasper/audio_io.py)
  aborts the ALSA output buffer for sub-50 ms cutoff.

This works because the discrimination window between "TTS-tail
probability" and "real speech probability" is large enough at
typical levels. The whole defense is that 0.06 gap.

### Where this approach is fragile

1. **Loud music under TTS reduces the gap.** When music is
   playing, both TTS bleed AND music vocals contribute to the
   mic. The VAD score floor rises. Real user speech needs to
   stand out further from the noise. Anecdotally, raised-voice
   barge-in still works; conversational-voice barge-in becomes
   unreliable.
2. **TTS amplitude isn't fixed.** [`TtsVolumeTracker`](../jasper/voice_daemon.py)
   boosts TTS gain when music is loud (it sits a configurable
   headroom above the music RMS). Louder TTS → more bleed in
   the mic → bleed score creeps toward 0.15.
3. **Server-side auto VAD is structurally off.** All three
   voice providers (Gemini Live, OpenAI Realtime, Grok) run with
   `automatic_activity_detection.disabled = true` via the
   manual-VAD path (see
   [HANDOFF-persistent-live-session.md](HANDOFF-persistent-live-session.md)).
   The server can't tell its own TTS from user speech, so
   re-enabling auto-VAD with the current mic substrate would
   bring back the self-interrupt loop the manual-VAD path was
   built to escape.
4. **No telemetry on barge-in success/fail rates today.** We
   don't have data on how often this actually breaks in real
   use. (See [Option C](#option-c--measure-first-vad-only-may-be-enough)
   below.)

### What "robust" barge-in would mean

Confidence that the user can interrupt the assistant mid-utterance
with a normal speaking voice, including while music is playing
underneath TTS. As a user-experience bar: comparable to ChatGPT
Voice / Alexa / Google Assistant.

That bar requires the mic signal — at the point where wake / VAD
/ server-side activity detection look at it — to be **clean of
the speaker's own output**. Which means AEC has to cancel both
music AND TTS, not just music.

---

## What the canonical AEC architecture says

Before any architectural choice, the canonical reference
architecture for AEC with multiple sound sources is worth
stating, because it constrains the rest of the discussion.

The professional installed-audio guidance and academic literature
on AEC converges on three properties:

1. **The reference signal must match exactly what was played
   through the speaker.** Anything that happens between the
   reference tap and the speaker (mixing, post-EQ, ducking,
   resampling) is invisible to the AEC and degrades cancellation.
2. **A single pre-mixed reference is the standard.** Multiple
   sources are mixed *before* the reference tap; the AEC sees
   one combined far-end signal.
3. **Delay between reference and capture must be tightly
   bounded.** AEC engines estimate this delay continuously but
   converge slowly and stay fragile if the underlying alignment
   wanders.

Citations from the research pass for this doc:

- [Switchboard Audio's AEC3 explainer](https://switchboard.audio/hub/how-webrtc-aec3-works/):
  *"if the reference signal doesn't match what was actually
  played through the speaker (e.g., because of post-processing
  or mixing after the reference tap point), AEC performance
  will suffer."*
- [Symetrix — Tips & Tricks for Successful AEC](https://www.symetrix.co/knowledge/tips-tricks-for-successful-aec/):
  *"The AEC reference point and local speaker outputs need to
  be tapped after all processing, and right before the outputs.
  …The AEC reference should receive a mix of all the far-end
  and program audio that will be played through the
  loudspeakers."*
- [Bose Professional AEC guide](https://pro.bose.com/en_us/support/article/aec_a_complete_guide.html)
  (lookup at write time returned 404; cached title and excerpt
  confirm same guidance — re-check if the canonical URL moves).
- [Biamp Tesira — Per-channel AEC referencing](https://support.biamp.com/Tesira/Programming/Per-channel_AEC_referencing):
  same pattern — one reference per AEC, drawn from the mixed
  output.

For the specific engine JTS uses (WebRTC AEC3 via
`libwebrtc-audio-processing-1`):

- AEC3 exposes `ProcessReverseStream()` for the far-end (single
  reference) and `ProcessStream()` for the near-end mic. There
  is no native multi-reference input.
- AEC3's delay estimator cross-correlates the single reference
  with the capture; if you feed it a software sum of two
  references with mismatched path delays, the estimator
  converges on a compromise that's wrong for both.
- See [issue 42221406 on the WebRTC tracker](https://issues.webrtc.org/issues/42221406)
  for community discussion of delay-estimator edge cases.

The Linux-world consensus matches: [PipeWire's
`module-echo-cancel`](https://docs.pipewire.org/page_module_echo_cancel.html)
creates a *sink* abstraction; every audio source writes to that
sink; the sink's content IS the reference. Same pattern as the
pro-audio guidance, packaged as a Linux module.

---

## Today's reality on JTS

The current topology, for reference. Full details in
[audio-paths.md](audio-paths.md).

```
MUSIC chain (gets CamillaDSP + main_volume ducking)
    renderers → private fan-in lanes:
                  librespot_substream   → hw:Loopback,0,0
                  shairport_substream   → hw:Loopback,0,1
                  bluealsa_substream    → hw:Loopback,0,2
                  usbsink_substream     → hw:Loopback,0,3
              → jasper-fanin sums lanes to hw:Loopback,0,7
              → snd-aloop capture side hw:Loopback,1,7
              → CamillaDSP
              → pcm.jasper_out (dmix on dongle)
              → dongle → amp → speakers

TTS chain (bypasses CamillaDSP)
    jasper-voice TtsPlayout → pcm.jasper_out (dmix on dongle)
                            → dongle → amp → speakers
```

The renderer-side dmix (`pcm.jasper_renderer_mix`, fronted by
`pcm.jasper_renderer_in`) was added in PR #214 (2026-05-22) to
let the three renderers hold the loopback simultaneously —
resolving the Spotify Connect handover bug. It was retired on
2026-05-26 after AirPlay validation showed the fan-in topology was
both cleaner and more reliable. **That retired dmix remains prior art
for the convergence sink pattern Option A below proposes — same idiom,
different location in the chain.**

The two chains converge at `pcm.jasper_out`, which is a dmix on
the Apple USB-C dongle. The AEC bridge's reference tap is
`pcm.jasper_ref` → `pcm.jasper_capture` → dsnoop on
`hw:Loopback,1,7` — the summed music chain, *upstream* of CamillaDSP
and *upstream* of the music↔TTS convergence point. So:

- AEC reference today contains music only.
- TTS is invisible to AEC.
- The convergence point (`jasper_out` dmix on hardware) cannot
  natively be snooped — ALSA dmix has no "tap its output"
  primitive.

This means the canonical fix (single pre-mixed reference taken
after the mixer) is **not achievable without changing the
topology.** That's the crux of the decision.

---

## The trap: "just put TTS in the AEC reference"

The obvious-looking fix is to tap TTS separately and add it to
the reference. Either:

- (A) Route TTS through the snd-aloop / CamillaDSP chain so it
  appears in the existing reference.
- (B) Add a second ALSA dsnoop on the TTS path; sum two
  references in the AEC bridge before feeding AEC3.
- (C) Use the ALSA `multi` plugin to fork TTS into both the
  dongle and the loopback.
- (D) Have `TtsPlayout` tee samples over UDP to the AEC bridge
  alongside the existing ALSA write.

**All four are wrong for the same underlying reason: they create
a reference that doesn't exactly match what was played.**

Each fails in a slightly different way:

- **(A)** breaks `TtsVolumeTracker`'s load-bearing assumption
  ("TTS bypasses CamillaDSP" — see
  [audio-paths.md](audio-paths.md) "Why TTS still tracks user
  volume changes"); subjects TTS to music ducking; requires
  either a second CamillaDSP instance or invasive CamillaDSP
  topology surgery (CamillaDSP supports only one capture per
  process).
- **(B)** re-introduces a second snd-aloop cable, which is
  exactly the failure class
  [HANDOFF-resilience.md](HANDOFF-resilience.md) PR #93
  eliminated. The `loopback_cable` kernel state still wedges on
  SIGKILL.
- **(C)** explicitly rejected in
  [audio-paths.md:18-24](audio-paths.md) — the ALSA `multi`
  plugin xrun-storms with bursty writers (TTS is bursty).
- **(D)** the most tempting; turns out to be the most fragile.
  Music ref path: ALSA capture from snd-aloop, low-latency,
  jitter-free. TTS ref path: UDP from `TtsPlayout`, variable
  jitter. **Different end-to-end path delays.** AEC3 estimates
  one delay; with mismatched path delays for the two summed
  sources, the estimator converges on a compromise that's
  wrong for both. Calibration drift over firmware/kernel/dongle
  changes turns this into a slow-rotting fragility.

Patents exist for hybrid / combined-reference AEC schemes (e.g.
[US9653060B1 — Hybrid reference signal for acoustic echo
cancellation](https://patents.google.com/patent/US9653060B1/en),
[US11477327 — Post-mixing acoustic echo
cancellation](https://patents.justia.com/patent/11477327))
precisely because doing this correctly is non-obvious signal
processing. JTS should not be inventing patentable DSP to enable
barge-in.

**Rule for future contributors:** if you find yourself proposing
to give AEC3 two references and sum them, stop. Read this section.
The single pre-mixed reference is the canonical answer; pick one
of the options below instead.

---

## Option A — Stay ALSA-only; add a software convergence sink

> **Policy status: rejected as speculative re-architecture.** Per the
> standing rule in [AGENTS.md](../AGENTS.md), changes that
> restructure the snd-aloop / dmix topology around AEC are not
> reviewable today. The costing below is preserved as decision-record
> only.

Restructure so music + TTS converge at a software mix point
*before* the dongle. The minimal shape:

```
renderers → existing music chain → CamillaDSP ──┐
                                                ├──> dmix(jasper_premix)
TTS ────────────────────────────────────────────┘        │
                                            snd-aloop sub2 (new)
                                                         │
                                          ┌──────────────┴──────────────┐
                                          ▼                             ▼
                            jasper-output-bridge          AEC bridge (ref input)
                                          │
                                   dongle dmix → DAC
```

Both CamillaDSP and `TtsPlayout` write to a new dmix
(`jasper_premix`), which sits on snd-aloop substream 2. A new
small always-on daemon (`jasper-output-bridge`) reads sub2
capture and writes to the dongle. The AEC bridge reads the same
sub2 capture as its reference. Single pre-mixed signal, perfect
time alignment (sample-locked dmix), canonical AEC architecture.

### What you'd build

Prior art is already in-tree: PR #214 added `pcm.jasper_renderer_mix`
as a multi-writer dmix in front of the loopback for renderer
convergence. This option uses the **same idiom one layer further
down the chain** — converging music + TTS *after* CamillaDSP.

1. New `pcm.jasper_premix` dmix definition in
   [`deploy/alsa/asoundrc.jasper`](../deploy/alsa/asoundrc.jasper)
   wrapping `hw:Loopback,0,2`. ipc_key 7780 (unique vs the existing
   7777=jasper_out, 7778=jasper_capture, 7779=jasper_renderer_mix).
2. Update [`deploy/camilladsp/v1.yml`](../deploy/camilladsp/v1.yml)
   so CamillaDSP's playback target is `jasper_premix` instead of
   `jasper_out`.
3. Update [`jasper/config.py`](../jasper/config.py)
   `tts_device` default to `jasper_premix`.
4. New daemon `jasper-output-bridge` (small Python or Rust):
   reads from `pcm.jasper_premix_capture` (dsnoop on
   `hw:Loopback,1,2`), writes to `pcm.jasper_out` (existing dmix
   on dongle). Mirrors the structure of `jasper-aec-bridge`,
   including `sd_notify` Tier 1+2 hardening from
   [`jasper/watchdog.py`](../jasper/watchdog.py).
5. Update [`jasper/cli/aec_bridge.py`](../jasper/cli/aec_bridge.py)
   `REF_DEVICE` to point at the new dsnoop on sub2.
6. Update [`audio-paths.md`](audio-paths.md) and
   [`HANDOFF-aec.md`](HANDOFF-aec.md) topology diagrams.

### Costs

- **One new always-on daemon.** ~30 MB Pss, comparable to
  jasper-aec-bridge. Needs Tier 1+2 watchdog.
- **~20-60 ms additional music latency.** One extra dmix hop
  (~10-20 ms) + the snd-aloop cable (~10-20 ms) + the output
  bridge's playback buffer (~10-20 ms). Depending on
  `period_size` tuning. Most music sources tolerate this;
  Bluetooth A2DP with video sync is the most sensitive.
- **Expanded snd-aloop kernel-state surface.** A second cable
  (sub2) joins the existing music chain cable (sub0) as wedge
  risk. If `jasper-output-bridge` is SIGKILL'd, sub2 wedges and
  audio stops until `rmmod snd_aloop && modprobe` (with all
  consumers stopped). Tier 4 in
  [HANDOFF-resilience.md](HANDOFF-resilience.md) becomes more
  likely to need wiring.
- **One more thing to think about during install / deploy /
  reconcile.** A new unit, new asoundrc clause, new failure
  mode in `jasper-doctor`.

### What it preserves

- `TtsVolumeTracker` continues to work — TTS still bypasses
  CamillaDSP (it joins at `jasper_premix`, not inside
  CamillaDSP), so the "TTS doesn't get ducked" property
  survives, and `TtsVolumeTracker`'s `playback_rms` observation
  source (CamillaDSP's `levels.playback_rms()`) is still valid
  for matching TTS gain to music loudness.
- Pure ALSA stack — no new audio server, no PipeWire migration.
- Stays inside the architectural framework `HANDOFF-resilience.md`
  established (sd_notify watchdog, fault-isolated daemons,
  UDP-localhost for mic transport).

### When this is the right answer

- You want barge-in robustness *and* you want to keep ALSA as the
  audio substrate.
- You're willing to take on one more daemon + one more snd-aloop
  cable in exchange for canonical AEC architecture.
- The latency cost (~20-60 ms on music) is acceptable.

---

## Option B — Migrate to PipeWire

> **Policy status: rejected by name.** [AGENTS.md](../AGENTS.md)
> "Architecture is fixed; swap the engine, not the topology"
> explicitly names "PipeWire `module-echo-cancel`" and "replacing
> snd-aloop with PipeWire fanout" as paths not to propose.
> [CONTRIBUTING.md](../CONTRIBUTING.md) "Working on a sensitive
> subsystem" repeats the constraint for external contributors.
> The costing below is preserved as decision-record only — if
> future evidence ever warrants reopening the conversation, the
> trade-off table is here.

PipeWire's `module-echo-cancel` does Option A out of the box,
plus several second-order wins JTS would otherwise build
incrementally.

### What you'd build

1. Install `pipewire`, `wireplumber`, `pipewire-pulse`, and
   `libspa-aec-webrtc` packages on the Pi.
2. Stop using `/root/.asoundrc` for routing; declare a PipeWire
   graph (in `~/.config/pipewire/` or
   `/etc/pipewire/pipewire.conf.d/`) with:
   - A virtual sink (`jasper-premix`) that all renderers and TTS
     write to.
   - `module-echo-cancel` consuming `jasper-premix` as the
     reference and the XVF chip's mic as the capture, exposing
     an echo-cancelled source for `jasper-voice`.
   - A loopback from `jasper-premix` to the dongle's hardware
     PCM.
3. Reconfigure renderers (`shairport-sync.conf`, `librespot.service`,
   `bluez-alsa-aplay.service`) to write to the PipeWire sink
   (typically via `pipewire-pulse` which presents a Pulse
   server API, or directly as PipeWire clients).
4. Update [`deploy/camilladsp/v1.yml`](../deploy/camilladsp/v1.yml)
   — CamillaDSP supports PipeWire as both a capture and a
   playback backend.
5. Retire `jasper-aec-bridge` — the
   `libspa-aec-webrtc` library inside PipeWire handles this.
6. Retire `pcm.jasper_capture`, `pcm.jasper_ref`,
   `pcm.jasper_out`, the snd-aloop module entirely.
7. Update or rewrite: [`audio-paths.md`](audio-paths.md),
   [`HANDOFF-aec.md`](HANDOFF-aec.md),
   [`HANDOFF-resilience.md`](HANDOFF-resilience.md),
   [`HANDOFF-airplay.md`](HANDOFF-airplay.md),
   `BRINGUP.md`, `install.sh`, and the various wizards that
   touch audio.
8. Retest *everything* on the new substrate. Every renderer,
   every voice provider, ducking, volume, AEC, AirPlay sync,
   Bluetooth A2DP latency, wake event capture.

### Second-order wins this gets you

These are real and worth weighing:

- **Solves the multi-mic arbitration problem cleanly.**
  PipeWire's `module-combine-stream` is the canonical primitive
  for the planned multi-satellite-mic feature documented in
  [satellites.md](satellites.md). Today's plan is to roll a
  custom arbitration daemon; PipeWire reduces that to graph
  config.
- **Better resampler defaults.** PipeWire's SPA resampler is
  generally on par with libsamplerate-best and benefits from
  more recent tuning than the ALSA defaults. The 12 dB 4-8 kHz
  loss in shairport's plug-resampler that motivated PR #75 is
  exactly the kind of foot-gun PipeWire would have caught
  earlier.
- **Bluetooth A2DP comes from PipeWire-native code path.**
  Retires `bluez-alsa-aplay` and the per-DAC asoundrc
  contortions for it.
- **Foundation for future audio integrations.** Snapcast,
  multi-room, network audio — PipeWire is where the Linux audio
  ecosystem is going, and these get easier on it.
- **Declarative routing graph.** The current asoundrc + multiple
  systemd units + ad-hoc snd-aloop topology becomes one config
  file describing the graph.

### Costs

- **~50-80 MB additional Pss** for `pipewire` + `wireplumber` +
  `pipewire-pulse`. (Verify on actual Pi 5 hardware before
  committing — published numbers vary by distro.) JTS today
  sits around 770 MB / 2 GB; PipeWire would land around 820-850
  MB. On the 1 GB Pi build, this matters more.
- **Multi-week project.** Replacing the audio substrate
  touches install, deploy, BRINGUP, doctor, every renderer, the
  AEC bridge, CamillaDSP integration, every wizard that touches
  audio, every test. Honest estimate: 2-4 weeks of focused work,
  longer if regressions surface.
- **Different mental model.** ALSA + asoundrc is "config files
  that processes evaluate locally." PipeWire is "a graph
  evaluated by a daemon that owns the audio path." Foreign
  idiom for current JTS, which is otherwise composed of small
  process-per-purpose daemons.
- **One more long-running daemon (and its session manager) in
  the resilience ladder.** PipeWire and wireplumber both need
  to be tracked. Their failure modes are different from
  ALSA's. The community has shaken out most of the early bugs
  but you'd be inheriting a new dependency surface.
- **Migration risk.** Any of the audio properties JTS spent
  effort tuning — the precise resampler choice, the per-renderer
  format negotiation, the AirPlay sync behaviour, the CamillaDSP
  format lock — needs to be re-validated. Some of these have
  PRs documenting subtle bugs already fixed; some of those
  bug-classes have PipeWire-side equivalents to discover.
- **Re-derives the resilience story.** The Tier 1-5 ladder in
  [HANDOFF-resilience.md](HANDOFF-resilience.md) was built
  against ALSA failure modes. PipeWire has its own failure
  modes (graph deadlocks, wireplumber restarts, RTKit
  priorities). Some Tier 1-2 work would re-apply; some would
  need fresh design.

### Comparison snapshot

| Property | Stay ALSA (Option A) | Migrate PipeWire (Option B) |
|---|---|---|
| Canonical AEC architecture | Yes (with new daemon + new cable) | Yes (out of box) |
| Multi-mic arbitration foundation | Custom code | `module-combine-stream` |
| Resource cost | +~30 MB Pss | +~50-80 MB Pss |
| Implementation effort | Days–1 week | 2-4 weeks + retest |
| Migration risk | Localized (one new daemon) | Replaces audio stack |
| Match to current JTS idiom | High | Low |
| New dependency surface | None | PipeWire + Wireplumber + SPA |
| Long-term ecosystem fit | Diverges from Linux mainstream | Aligns with where Linux audio is going |

### When this is the right answer

- Barge-in robustness has been measured to actually matter (see
  Option C).
- You're also seeing the multi-renderer contention bug in real
  use, the multi-mic-satellite arbitration is on the near
  roadmap, and the ALSA-only patchwork is starting to feel like
  it's accumulating workarounds rather than solving root causes.
- You have 2-4 weeks of focused time and willingness to retest
  the full audio chain.

### Important nuance on the existing exclusion

[HANDOFF-resilience.md](HANDOFF-resilience.md) lists "PipeWire
migration — out of scope per project policy" in its "what we
explicitly did NOT do" section. The reasoning quoted:

> The resilience win comes from removing snd-aloop from the
> bridge↔voice path entirely, not from replacing the userspace
> audio stack.

That exclusion was scoped to a specific resilience question (do
we need PipeWire to fix the snd-aloop bridge wedge?) and the
answer was correctly "no, UDP is enough." The exclusion was not
a general "PipeWire is forever inappropriate for JTS." Barge-in
is a different motivation. Re-evaluating the trade-off is a
legitimate move; it doesn't contradict prior decisions.

---

## Option C — Measure first; VAD-only may be enough

The recommended *immediate* next step. Before committing to
either A or B, gather evidence on whether the current VAD-only
barge-in is actually insufficient under real use.

### What you'd build

Less than the first version of this doc implied — much of the
measurement substrate already exists or is in active development
under [HANDOFF-mic-quality-v2.md](HANDOFF-mic-quality-v2.md). The
barge-in story extends that program rather than building parallel
infrastructure. Concrete additions:

1. Per turn during TTS playback, log every frame where Silero VAD
   score crosses an "attempted barge-in" threshold (e.g. > 0.10,
   below the 0.15 gate, to catch near-misses too). Live in
   [`jasper/voice_daemon.py`](../jasper/voice_daemon.py) alongside
   the existing in-session VAD plumbing at line ~2365.
2. For each such moment, record: TTS RMS at the moment, music
   RMS at the moment, whether the gate (≥0.15) actually fired,
   time from gate fire to `tts.flush()` completion, and whether
   the user re-spoke within ~5 s (proxy for "first attempt
   failed, user tried again").
3. Capture short audio clips of the mic and the AEC reference
   around each attempt — extend the existing wake-events capture
   ring buffers in [`jasper/wake_events.py`](../jasper/wake_events.py)
   with a "barge-in" event type. Reuses the SQLite schema and the
   500 MB rolling audio retention from
   [HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md).
4. Run for two to four weeks of normal household use.
5. Use the existing `bash scripts/fetch-wake-events.sh` flow plus
   a small query script to summarise. The capture / scoring
   tooling being developed under PR #206 (mic-quality-v2) is the
   natural place to add barge-in-specific scoring queries —
   coordinate so the indexes generalize across both workstreams
   rather than diverge.

### What the data tells you

Three buckets the data is likely to fall into:

- **VAD-only is already adequate.** Most barge-in attempts get
  a confident score (~0.3+), gate fires promptly, no
  re-attempts. You've saved yourself a multi-week project.
- **VAD-only is adequate *except* in specific conditions.**
  Failure cluster is tightly correlated with one or two
  conditions (very loud music, particular TTS phonemes,
  specific user voices). May be fixable with targeted tuning
  (per-music-level threshold, different VAD model, longer
  refractory) — cheaper than full architectural change.
- **VAD-only is genuinely insufficient.** Failure rate is high
  enough across normal conditions that architectural rework is
  justified. Now you have real evidence to choose A vs B with,
  rather than speculation.

### Why this is the recommended first move

- Costs roughly a day of instrumentation work.
- Defers a 2-4 week decision until you have data.
- Matches the JTS pattern from
  [HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md):
  measure before re-architecting. The shipped wake-event
  capture system exists exactly for this kind of "is the thing
  we think is broken actually broken, and how badly" question.
- The instrumentation itself is durable. Even after a barge-in
  architectural change, having the success-rate metric makes
  the change's improvement (or regression) measurable.

---

## Other alternatives considered (and mostly rejected)

For completeness, things that came up in the research pass and
didn't survive scrutiny.

### Hardware AEC, revisited

> **Policy status: rejected by name.** [AGENTS.md](../AGENTS.md)
> names "dual-USB-sink hardware-AEC retry" and "custom XVF firmware"
> as paths not to propose. The notes below remain as historical
> record of why the trade was costed and declined.

The XVF3800's on-chip AEC was disabled deliberately
([HANDOFF-aec.md](HANDOFF-aec.md): the chip's AEC assumed the
chip drove the speaker via its own codec, which JTS doesn't —
audio routes through the Apple dongle). A topology change that
returned the speaker drive to the chip's codec would re-enable
chip AEC and solve barge-in cleanly. But the dongle was chosen
for DAC quality; the chip's AIC3104 is meaningfully worse. Hard
to imagine this trade landing as positive.

The convergence question (does chip AEC actually adapt in the
*current* dongle topology when fed a USB-IN reference signal?)
has its own user-authorized carve-out, **also currently shelved**:
see [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md) and the four
`scripts/chip-aec-*.sh` scripts. They live on `main` as dormant
infrastructure — production state is untouched until `bash
scripts/chip-aec-setup.sh` runs; `chip-aec-teardown.sh` reverts.
Not on the roadmap; preserved so we don't have to re-derive the
question if AEC3 ever plateaus. **That carve-out is scoped to the
convergence test only**: it does not re-open the codec-swap
dismissal above, nor PipeWire `module-echo-cancel`, dual-USB-sink,
or custom firmware. Agents proposing those remain bound by the
policy.

### Different AEC engine

SpeexDSP's AEC is the main alternative. It's also single-
reference (see [Speex EchoState docs](https://www.speex.org/docs/api/speex-api-reference/group__SpeexEchoState.html))
— it would face the same multi-source problem. AEC3 is the
better engine on most measures; switching engines doesn't help.

### Train a custom VAD

A purpose-built VAD that's better at distinguishing JTS-TTS
specifically from user speech. Real but expensive — would need
a labelled dataset of "TTS vs user during TTS" pairs, model
training, on-device inference budget. Buys some improvement over
the off-the-shelf Silero model without changing the AEC
architecture, but the ceiling is still "we can't see the TTS
signal cleanly." Not a substitute for the architectural fix
when conditions push the VAD's discrimination window past its
limit.

### Push-to-talk barge-in via the dial

The rotary dial has a "hold to talk" Gemini session. A
variation: while TTS is playing, holding the dial drops TTS
volume to zero AND opens an interrupt path. Reliable, but
trades the no-physical-interaction property of voice barge-in
for a button press. Worth offering as a complementary capability,
not a substitute.

### Mic mute during TTS

Don't stream mic frames at all during TTS. Trivially eliminates
self-wake and the discrimination problem. Also trivially
eliminates barge-in. Anti-feature.

### JACK instead of PipeWire

JACK is a pro-audio routing daemon with native multi-writer
sinks. Pre-dates PipeWire by years. Active community, but it's
oriented toward low-latency studio use, not embedded smart-home
audio. PipeWire absorbed most of JACK's capabilities and added
sensible defaults for consumer use cases. JACK is the worse fit
for JTS for the same reasons PipeWire is the canonical Linux
audio answer in 2026.

### Single-purpose UDP-based mixer daemon

A custom small daemon that owns the dongle, accepts music
(snd-aloop or UDP) and TTS (UDP), mixes them, writes to the
dongle, and forwards the mix to the AEC bridge over UDP. This
is "Option A done with UDP instead of a second snd-aloop
cable" — keeps the canonical single-reference architecture and
avoids the kernel-state wedge risk. But CamillaDSP doesn't
speak UDP; you'd need an intermediate process to ferry
CamillaDSP's ALSA output into the UDP mixer, which itself
re-introduces some of the failure surface. Possibly worth
exploring if Option A's snd-aloop sub2 cable proves to be the
specific thing that wedges, but not the obvious starting point.

---

## Open questions

The decision can't be made cold. Before either Option A or
Option B lands, these need answers:

1. **What's the actual barge-in success rate today?** Required.
   Drives whether this work is needed at all. → Option C.
2. **Is the failure mode concentrated in specific conditions?**
   If yes, may admit a tuning fix rather than architectural
   change. → Option C.
3. **What's PipeWire's real Pss on a Pi 5 running JTS?** The
   50-80 MB number is a literature estimate; measure on actual
   hardware (boot a test image with PipeWire installed, run
   nothing else, take `smem`). Drives the "fits on 1 GB?"
   question.
4. **Does CamillaDSP's PipeWire integration work cleanly in
   2026?** CamillaDSP gained PipeWire support a few years ago;
   verify it's stable and that the `main_volume` ducking still
   works the way `Ducker` expects.
5. **What's the actual latency budget for music?** AirPlay 2 is
   sync-tolerant; Bluetooth A2DP-with-video is not. Measure
   AirPlay sync drift and BT lip-sync delta with a 60 ms
   playback latency increase before committing to Option A.
6. **What's the migration path for the wake-event corpus and
   the regression test scenarios?** A PipeWire migration
   shouldn't invalidate the labelled wake-event corpus or the
   voice-eval regression suite. Sanity-check that AEC reference
   format / mic capture format / sampling rate stay compatible.
7. **Does the household actually use barge-in?** If the user
   never tries to interrupt TTS in real life (because TTS turns
   are short, because they wait), the whole question is
   academic. Option C surfaces this.

---

## Recommendation

The production fan-in topology resolved the renderer contention and
retired the renderer-side dmix from PR #214. What remains is the
barge-in question itself. The path that stays inside the standing
policy is:

1. **Build Option C instrumentation.** ~1 day. Extends the
   mic-quality-v2 measurement substrate ([HANDOFF-mic-quality-v2.md](HANDOFF-mic-quality-v2.md))
   rather than building parallel infrastructure. Run for 2-4
   weeks. Get real data on how often VAD-only barge-in actually
   fails under normal household use.
2. **Based on the data**, choose:
   - **If VAD-only is adequate:** declare barge-in done, archive
     this doc with a "resolved 2026-XX: VAD-only met the bar"
     note. No architectural change needed.
   - **If VAD-only is inadequate but the failure cluster is
     narrow:** try targeted VAD tuning, engine-internal AEC3
     knob changes ([AGENTS.md](../AGENTS.md) "AEC bridge —
     reconciler toggle"), or single-knob OS-layer fixes that
     measurement has localized to a specific layer. All in policy.
   - **If VAD-only is genuinely insufficient and engine-internal
     tuning hits a ceiling:** the policy question reopens. At that
     point, the Option A / Option B costing in this doc is the
     starting record for whether the trade has changed enough to
     reconsider. **Reopening the policy is the user's call, not
     an agent's.** Surface the data and the trade; don't propose
     the architecture change.

The decision is not urgent and shouldn't be made on speculation.
The data-collection step is the highest-leverage move and the
only one currently in-policy.

---

## References

External sources surveyed for this doc:

- [Switchboard Audio — How WebRTC AEC3 Works](https://switchboard.audio/hub/how-webrtc-aec3-works/)
- [PipeWire — module-echo-cancel documentation](https://docs.pipewire.org/page_module_echo_cancel.html)
- [PipeWire — module-loopback documentation](https://docs.pipewire.org/page_module_loopback.html)
- [Symetrix — Tips & Tricks for Successful AEC](https://www.symetrix.co/knowledge/tips-tricks-for-successful-aec/)
- [Bose Professional — AEC: A Complete Guide to Reference](https://pro.bose.com/en_us/support/article/aec_a_complete_guide.html) (URL was returning 404 at write time — search for current canonical URL if revisiting)
- [Biamp — Per-channel AEC referencing](https://support.biamp.com/Tesira/Programming/Per-channel_AEC_referencing)
- [XMOS — Choosing an Acoustic Echo Canceller for voice-enabled smart home products](https://www.xmos.com/developer/blog/huw/post/choosing-acoustic-echo-canceller-voice-enabled-smart-home-products)
- [voice-engine/ec — Echo Canceller for Linux on Pi (uses SpeexDSP)](https://github.com/voice-engine/ec)
- [WebRTC AEC3 capture signal delay tracker issue](https://issues.webrtc.org/issues/42221406)
- [Speex EchoState API reference](https://www.speex.org/docs/api/speex-api-reference/group__SpeexEchoState.html)
- [Hybrid reference signal for AEC — US9653060B1](https://patents.google.com/patent/US9653060B1/en)
- [Post-mixing acoustic echo cancellation — US11477327](https://patents.justia.com/patent/11477327)
- [Multichannel acoustic echo cancellation — US9967661B1](https://patents.google.com/patent/US9967661B1/en)
- [Home Assistant Voice Preview Edition](https://www.home-assistant.io/voice-pe/)
- [Arch Linux Forums — snd-aloop documentation thread](https://bbs.archlinux.org/viewtopic.php?id=276688)

Internal cross-references (for the next reader):

- [audio-paths.md](audio-paths.md) — current routing topology,
  why TTS bypasses CamillaDSP, what `TtsVolumeTracker` compensates
  for.
- [HANDOFF-aec.md](HANDOFF-aec.md) — AEC engine choice, the
  chip-AEC-disabled investigation, current software AEC tuning.
- [HANDOFF-resilience.md](HANDOFF-resilience.md) — the resilience
  ladder, the snd-aloop wedge story, the PipeWire-exclusion
  framing (and its specific scope).
- [HANDOFF-persistent-live-session.md](HANDOFF-persistent-live-session.md)
  — why manual VAD is used today, the
  `automatic_activity_detection.disabled` decision, the
  NO_INTERRUPTION history.
- [HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md) — the
  capture-and-label pattern Option C would extend.
- [HANDOFF-mic-quality-v2.md](HANDOFF-mic-quality-v2.md) — active
  workstream building the measurement infrastructure Option C
  would extend rather than duplicate.
- [satellites.md](satellites.md) — multi-mic arbitration
  design that Option B would simplify.

---

Last verified: 2026-05-26.
