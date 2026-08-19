# Research: rate tracking across the ring — what absorbs drift when the bonded follower's ingress leaves snd-aloop

**Date:** 2026-08-17
**Worktree pinned:** `/Users/jaspercurry/Code/JTS/.claude/worktrees/loopback-retirement-ring-3a4b6e` @ `6e569e8dc8e572a8d648d332c414374b8394496e`
**Status:** research memo. Not a design, not a recommendation. Decision input only.

**Honesty legend used throughout.**
`[M]` measured — read directly from source, file:line given.
`[C]` cited — from an upstream document/issue, URL given.
`[I]` inferred — reasoning stated inline; not observed.
`[N]` absent — searched and not found; the search scope is named.

---

## 0. The one-paragraph answer

The question as posed — *"is `rate_adjust` still needed or even viable against a ring capture?"* — has a
mechanical answer that documents alone settle, and the answer is **no, not viable, by construction**.
CamillaDSP v4.1.3 can only tune a capture clock through one of two named ALSA control elements, both of
which require the capture PCM to have a hardware card index; every ALSA **ioplug** reports `card = -1`, so
neither element can ever be found on `jts_ring_*`. With no async resampler configured (JTS emits none
anywhere), the rate-adjust request then falls off the end of an `if/else if` chain and is **silently
discarded** — no warning, no log, while CamillaDSP's own reported `rate_adjust` status field keeps showing
the requested value. That is fully proven below from two upstream sources.

But the investigation also **falsified the brief's premise**. The brief states that the bonded active
follower "runs `rate_adjust: true` on an snd-aloop capture as its sole drift absorber". On a **ring-armed**
box that is already untrue: the follower's `enable_rate_adjust` is keyed off its *playback* device, its
playback device resolves to the ACTIVE ring **unconditionally**, and the ring branch returns
`enable_rate_adjust = False`. So today's production active follower already ships with rate-adjust **off**,
chunksize 128, target_level 128 — and the guard test that claims otherwise pins a fixture whose playback
device is a DAC. **The load-bearing unknown does not exist in the form the brief describes it.** What
Phase 1 actually changes is the ingress transport only. The real open questions move to snapclient's
tolerance of the ioplug, and they are different questions than the one asked.

---

## 1. Component mechanics

### 1.1 CamillaDSP v4.1.3 — what `enable_rate_adjust` actually does

**Version pin.** `deploy/install.sh:68-71` — `CAMILLA_VERSION="v4.1.3"`, prebuilt aarch64 release tarball,
SHA-256 pinned (`d9a17092923ebfe5d20a770c6b6a7eb2268f9700f999bf604b9db09f518aca5a`). Not built from source.
`[M]` Companion `pycamilladsp` is hash-pinned to git rev `fdc0d163e02dd73206a493402b43c83502ad83d7`
(`pyproject.toml:56-58`). `[M]` **Every upstream claim below is pinned to v4.1.3.**

**The control loop lives on the PLAYBACK side.** `src/alsa_backend/device.rs:527-530` constructs
`PIRateController::new_with_default_gains(samplerate, adjust_period, target_level)`. The measured variable
is the **playback** device's buffer level — `buf_manager.current_delay(avail)` plus queued chunks, averaged
over `adjust_period` (`device.rs:640-651`). The controller output is a *capture* speed, sent to the capture
thread as `StatusMessage::SetSpeed` (`device.rs:658-665`). `[M]`

So: **`target_level` is a playback-buffer setpoint; the actuator is the capture clock.** The repo's own
prose agrees — `docs/HANDOFF-airplay.md:320-323`: *"`enable_rate_adjust` PI controller tunes the snd-aloop
capture clock … to hold its playback buffer at `target_level`"*. `[M]`

**Controller characteristics** (`src/utils/rate_controller.rs:33-113`) `[M]`:

| Parameter | Value |
|---|---|
| `k_p` | 0.2 |
| `k_i` | 0.004 |
| `ramp_steps` | 20 |
| `ramp_trigger_limit` | 0.33 (re-ramp when \|target−level\|/target exceeds this) |
| error normalisation | `rel_err = (level − target) / (adjust_period × fs)` |
| **output clamp** | `output.clamp(-0.005, 0.005)` → **±5000 ppm (±0.5 %) maximum correction** |
| returned speed | `1.0 − output` |

**The three-tier actuator fallback — the decisive mechanism.** `src/alsa_backend/device.rs:896-917`,
verbatim `[M]`:

```rust
Ok(CommandMessage::SetSpeed { speed }) => {
    rate_adjust = speed;
    if let Some(elem_loopback) = &element_loopback {
        debug!("Setting capture loopback speed to {speed}");
        elem_loopback.write_as_int((100_000.0 / speed) as i32);
    } else if let Some(elem_uac2_gadget) = &element_uac2_gadget {
        debug!("Setting capture gadget speed to {speed}");
        elem_uac2_gadget.write_as_int((speed * 1_000_000.0) as i32);
    } else if let Some(resampl) = &mut resampler {
        if params.async_src {
            debug!("Setting async resampler speed to {speed}");
            ... set_resample_ratio_relative(speed, true) ...
        } else {
            warn!("Requested rate adjust of synchronous resampler. Ignoring request.");
        }
    }
}
```

**There is no final `else`.** If all three bindings are `None`, the arm does nothing and logs nothing. `[M]`

The two elements are found by name at `device.rs:780-793` `[M]`:

```rust
element_loopback   = find_elem(h, ElemIface::PCM, Some(device), Some(subdevice), "PCM Rate Shift 100000");
element_uac2_gadget = find_elem(h, ElemIface::PCM, Some(device), Some(subdevice), "Capture Pitch 1000000");
```

**The precondition that rules out every plugin PCM.** `device.rs:767-769` (and the same shape on the
playback side at `:512-514`, which carries the explanatory comment) `[M]`:

```rust
let card = pcminfo.get_card();
// Virtual devices such as pcm plugins don't have a hw card ID
// Only try to create the HCtl when the device has an ID
let hctl = (card >= 0).then(|| HCtl::new(&format!("hw:{card}"), true).unwrap());
```

**Upstream alsa-lib confirms `card = -1` for every ioplug.** `alsa-lib/src/pcm/pcm_ioplug.c:91-95` `[M]`:

```c
static int snd_pcm_ioplug_info(snd_pcm_t *pcm, snd_pcm_info_t *info)
{
	memset(info, 0, sizeof(*info));
	...
	info->card = -1;
```

Our plugin is a genuine ioplug: `c/jts-ring-ioplug/pcm_jts_ring.c:148` includes `<alsa/pcm_external.h>`,
`:169` declares `snd_pcm_ioplug_t io;`, and both callback tables are `snd_pcm_ioplug_callback_t`
(`:906-941`). `[M]`

**Chain of consequence** `[I from the four `[M]` facts above, no step unobserved]`:
`jts_ring_capture` is an ioplug → `snd_pcm_info.card == -1` → CamillaDSP builds no `HCtl` →
`element_loopback` and `element_uac2_gadget` are both `None` → JTS emits no `resampler` block anywhere
(§1.2) → the `SetSpeed` arm matches nothing → **rate adjust is a silent no-op on any `jts_ring_*` capture.**

**Two observability traps that follow.**

1. **`capture_status.rate_adjust` is the REQUEST, not the applied value.** `device.rs:897` sets
   `rate_adjust = speed` at the top of the arm, before any branch; `:972` publishes
   `capture_status.rate_adjust = rate_adjust as f32`. `[M]` So the websocket/`getcaptureraterate`-style
   status field will move plausibly while nothing is being actuated. **A spike cannot use this field as
   evidence that rate adjust works.**
2. **The only honest signal is a log line that is present or absent.** `device.rs:828-838` `[M]`:
   ```rust
   if element_loopback.is_some() || element_uac2_gadget.is_some() {
       info!("Capture device supports rate adjust");
       ...
   }
   ```
   There is **no `else` warning branch.** `[M]` So *absence* of `Capture device supports rate adjust` at
   INFO is the observable proof that clock tuning is unavailable. The repo has already used this signal:
   the S0-sync bench recorded *"camilla logs `Capture device supports rate adjust`"* as its pass evidence
   on the aloop path (`docs/HANDOFF-distributed-active.md:838-845`). `[M]`

**Upstream README agrees with the source.** The v4.1.3 README documents the same three tiers in prose —
clock tuning "supported on ALSA Loopback and USB Audio Gadget on Linux, and BlackHole 0.5.0+ on macOS";
otherwise async-resampler ratio; otherwise *"Rate adjust requests cannot be applied, so independent clocks
will still drift over long runtimes."* `[C]`
<https://raw.githubusercontent.com/HEnquist/camilladsp/v4.1.3/README.md>

**Changelog context:** v3.0.0 — *"Improved controller for rate adjustment."* So v4.1.3 carries the newer
controller, not the one any pre-v3 field report describes. `[C]`
<https://github.com/HEnquist/camilladsp/blob/master/CHANGELOG.md>

---

### 1.2 The snd-aloop oscillation record — what it actually says

**Predicate:** `jasper/camilla_config_contract.py:317-397`,
`snd_aloop_rate_adjust_oscillation_reason(text) -> str | None`. `[M]`

Docstring, load-bearing sentences verbatim:

> *"A snd-aloop ALSA capture (``plug:jasper_capture`` / ``hw:Loopback,...``) at capture-rate ==
> playback-rate already rate-tracks via the loopback, so ``enable_rate_adjust: true`` WITH an async
> resampler makes CamillaDSP's adjuster and the resampler fight … **The safe shape is enable_rate_adjust
> true AND NO async resampler block.**"*

> *"This is a TEST-TIME contract predicate, NOT a runtime emit-time guard: it has no callers in the emit
> path, so it does not fail-loud at config generation."*

**The oscillation's precondition is a PAIR, not `rate_adjust` alone.** The predicate returns non-`None`
only when `has_resampler and is_async_resampler(resampler_type)`
(`camilla_config_contract.py:390-397`). `[M]` It explicitly *declines* to flag `rate_adjust` on aloop by
itself, and explicitly declines to flag `enable_rate_adjust: false` on an aloop capture (the
bonded-leader pipe-sink case). `[M]`

**Narrative:** `docs/HANDOFF-airplay.md:460-527`, "Pattern A", marked *fixed in PR #75*. `[M]`
Config that oscillated: capture `plug:jasper_capture` (aloop) @48 k, playback `outputd_content_playback`
(also aloop) @48 k — capture rate == playback rate — with **both** `enable_rate_adjust: true` **and**
`resampler: {type: AsyncSinc, profile: Balanced}`. Symptoms: glitches every ~5–15 s; `Capture read short`
on essentially every chunk ("977 frames instead of the requested 1024"); `Prepare playback after buffer
underrun` every ~5 s; and CamillaDSP's own startup warning `Needless 1:1 sample rate conversion active.
Not needed since capture device supports rate adjust`. `[M]`

**Mechanism — this is where the repo's record and the upstream source diverge, and it matters.**
The repo attributes it to *"two drift controllers fighting each other"*, citing
[HEnquist/camilladsp#207](https://github.com/HEnquist/camilladsp/issues/207). `[M/C]` But at v4.1.3 the
`SetSpeed` arm is an `else if` chain: when `element_loopback` is found (which it is, on aloop), the
resampler branch is **never reached**. `device.rs:898-906`. `[M]` CamillaDSP drives exactly **one**
actuator. So "two controllers fighting for the ratio" is not literally what happens.

What the evidence *does* support `[I, reasoning stated]`: the AsyncSinc sits in the signal path at ratio
≈1.0 with its own variable input demand and internal sinc-kernel buffering. The PI controller's measured
variable is the playback buffer level, which now includes that variable-latency stage, and the capture
read size is set by the resampler's per-chunk demand rather than a fixed chunk. That gives the control
loop extra lag in its plant and a variable-size capture request against a clock the loop is itself
shifting — which is consistent with the observed chronic short reads. The repo's own hedge is accurate:
its `docs/HANDOFF-airplay.md` symptom list is empirical, the causal claim rests on the upstream issue plus
CamillaDSP's warning line, and **no in-repo measurement of the loop gain exists.** `[M]`

**Bottom line for Phase 1:** the oscillation record's precondition — *an async resampler present on an
aloop capture* — **cannot occur on a ring capture**, because tier 1 is unavailable there *and* JTS emits
no resampler anywhere. The record therefore does not transfer to the ring in either direction: it is
neither a reason to fear rate-adjust-on-ring nor evidence about it. The proof that rate-adjust-on-ring is
inert is §1.1, not this record.

**Correction to the repo's stated reason.** `jasper/active_speaker/camilla_yaml.py:137-153` justifies
`RING_CAMILLA_ENABLE_RATE_ADJUST = False` as: *"a blocking slot handshake gives the rate controller nothing
to adjust TO, and rate_adjust over an snd-aloop-class transport is a documented oscillation shape in this
repo."* `[M]` The first clause is sound. The second is a mis-attribution: the documented oscillation
requires a resampler, and the ring is not "snd-aloop-class" for this purpose — it is *weaker*, because it
cannot be clock-tuned at all. **The shipped setting is right; one of the two stated reasons is not.**

---

### 1.3 The doctor invariant (inv-5) — and a phrase that does not exist

`jasper/cli/doctor/grouping.py:236-283`, `check_grouping_rate_adjust`. `[M]`

Scope: **active bond LEADER only.** Every other role returns `ok` / *"not an active bond leader (n/a)"*.
Docstring, verbatim on the follower:

> *"an ACTIVE follower's CamillaDSP IS in the bonded path … but is itself the sole rate-tracker of that
> loopback, so ``rate_adjust: true`` is REQUIRED there, not forbidden."*

It never `fail`s — only `ok` or `warn`, on four paths. `[M]` Note a fail-soft asymmetry: the reader
returns `None` for *absent or unparseable*, and the check tests `if rate_adjust is True`, so a config with
**no** `enable_rate_adjust` key reports `ok` with the message *"rate_adjust off for active leader"* — a
claim the file does not support. `[M]` Observation only; not in scope to fix here.

**The brief's quoted phrase "exactly one rate-tracker per clock domain" does not exist in the repo.**
Searched case-insensitively for `exactly one rate`, `per clock domain`, `rate-tracker per`, `tracker per
clock`. `[N]` The three real phrasings are:
- `docs/HANDOFF-multiroom.md:630-631` — *"**Exactly one rate-adjuster per chain.**"* (per **chain**)
- `jasper/cli/doctor/grouping.py:241-242` — *"the single rate-tracker for the synced chain"*
- `docs/HANDOFF-audio-latency-foundation.md:69` — *"Give every foreign clock exactly one rate matcher"*
  (per **foreign clock**)

The distinction is not pedantic: "per chain" and "per foreign clock" both permit **zero** rate-trackers in
a segment that has no clock crossing, which is precisely the claim Architecture B rests on.

---

### 1.4 The `jts_ring` ioplug — writer-facing facts

Source: `c/jts-ring-ioplug/pcm_jts_ring.c`, `jts_ring_shm.c`, `jts_ring_shm.h`. All `[M]` unless tagged.

#### Facts table

| Fact | Value | Cite |
|---|---|---|
| `.transfer` return | **always `size`** — never short, never negative | `pcm_jts_ring.c:404-434`, esp. `:433` |
| publish result | **discarded** — drops are invisible to the writer | `pcm_jts_ring.c:428` |
| full + **live** reader | `nanosleep` loop, ≤32 ticks, then **discard NEWEST** (no memcpy, `write_seq` not advanced) | `jts_ring_shm.c:1068-1078` |
| full + **dead** reader | **overwrite OLDEST** immediately (writer advances `read_seq` itself) | `jts_ring_shm.c:1062-1067` |
| sleep primitive | plain `nanosleep`, period/4, capped 2 ms, floor 1 µs. **No futex** (reserved, future work) | `jts_ring_shm.c:237-246`; `:238-239` |
| max in-callback stall @128 frames | 32 × 0.667 ms ≈ **21.3 ms**, then drop | `[I]` arithmetic on `:148`, `:240-242` |
| `full_waits` | counts publish *calls* that waited ≥1 tick, not ticks | `jts_ring_shm.c:1068` |
| `full_waits` visibility | **close-time `SNDERR` line only** — not in `/state` for a C writer | `pcm_jts_ring.c:536-539` |
| `SND_PCM_NONBLOCK` | **ignored** — `io->nonblock` never read | `pcm_jts_ring.c:1133` + `[N]` |
| **`.delay`** | `occupancy_slots × period_frames + stage_frames` — **ring fill ONLY** | `pcm_jts_ring.c:455-458` |
| `.delay` downstream term | **none** — no reader-buffer, DAC, or consumer latency term | `pcm_jts_ring.c:440-460` |
| max reportable delay @shipped | 2×128 + 127 = 383 frames ≈ **8.0 ms** | `[I]` arithmetic |
| `.pointer` | `appl_frames − in_flight`, `in_flight = occupancy×period + stage` | `pcm_jts_ring.c:386-401` |
| **back-pressure genuine?** | **YES while reader heartbeat fresh** — `occupancy = write_seq − read_seq`, `read_seq` Released only by the real reader | `jts_ring_shm.c:1101-1105`, `:1297-1299` |
| back-pressure when reader stale (>2 s) | **NO** — `in_flight` collapses to `stage` only; `avail` reads ~full forever | `jts_ring_shm.h:566-604` |
| pointer forward clamp | ≤ `buffer_size − period` per call = **128 frames at n_slots=2** | `jts_ring_shm.h:583-602` |
| `.avail` callback | none — alsa-lib derives from `.pointer` | `[N]` callback tables `:906-941` |
| timestamps | **zero occurrences** of `htimestamp` / `audio_tstamp` / `SND_PCM_TSTAMP` | `[N]` all three plugin files |
| `-EPIPE` / xrun to client | **never signalled** — zero `EPIPE`/`ESTRPIPE`/`XRUN` in any plugin C file | `[N]` |
| POLLOUT gate | withheld iff full **and** reader live; granted if space **or** reader dead | `pcm_jts_ring.c:881-904`; `jts_ring_shm.c:1112-1120` |
| poll fd | bare repeating **timerfd** at period/4 (~0.667 ms → ~1500 Hz), not a ring-state event | `pcm_jts_ring.c:866-879`, `:274-283` |
| `.pause` / `.resume` / rewind / `.sw_params` | **not implemented** | `[N]` `:906-941` |
| `.drain` | zero-pads + publishes partial slot, then bounded wait `n_slots×8` ticks ≈ 10.7 ms | `pcm_jts_ring.c:476-509` |
| frame quantisation at the API | **none** — any write size accepted; only whole slots cross to the reader | `pcm_jts_ring.c:417-431` |

#### hw_params — every dimension single-valued except access

`jts_ring_set_hw_constraints`, `pcm_jts_ring.c:943-1007`. `[M]`

| Param | Constraint |
|---|---|
| `HW_ACCESS` | playback: `{RW_INTERLEAVED, MMAP_INTERLEAVED}`; capture: `{RW_INTERLEAVED}` |
| `HW_FORMAT` | **single** — `S16_LE` or `S32_LE` per conf.d |
| `HW_CHANNELS` | **single** — `minmax(channels, channels)` |
| `HW_RATE` | **single — 48000** |
| `HW_PERIOD_BYTES` | **single** — `period_frames × frame_bytes` |
| `HW_PERIODS` | **single** — `n_slots` |
| `HW_BUFFER_BYTES` | not set; falls out as `periods × period_bytes` `[I]` |

Plugin's own comment (`:974-979`): *"Exactly ONE format and ONE channel count … Advertising a single value
keeps the app's negotiated hw_params identical to the ring's geometry by construction."* `[M]`

`jts_ring_hw_params` is a no-op that always returns 0 (`:511-518`); refusal happens in alsa-lib's refine
stage. `[M]` Shipped conf.d PCMs are **raw `type jts_ring`, not `plug`-wrapped**
(`deploy/alsa/conf.d/60-jts-ring.conf:97-163`) — no rate or format conversion on a direct open. `[M]`

#### Geometry

Shipped: `period_frames 128`, `n_slots 2`, `format S32_LE`
(`deploy/alsa/conf.d/60-jts-ring.conf:108-114`). `[M]` Bounds `JTS_RING_MIN_SLOTS 2` /
`JTS_RING_MAX_SLOTS 16` (`jts_ring_shm.h:71`, `:90`), enforced with a clean `-EINVAL`
(`pcm_jts_ring.c:1098-1101`). `[M]` Geometry is per-PCM-instance configurable from the ALSA conf; any
unknown field is refused `-EINVAL` (`:1090-1091`). `[M]`

| Quantity | Frames | ms @48 k |
|---|---|---|
| period | 128 | **2.667** |
| buffer @ `n_slots=2` | 256 | **5.333** |
| buffer @ `n_slots=16` | 2048 | **42.667** |

`[I]` arithmetic; the 5.3 ms figure is stated independently at `60-jts-ring.conf:46-47`. `[M]`

#### Exclusivity and liveness

Writer holds `flock(LOCK_EX|LOCK_NB)` on `<ring>.writer.lock` for the life of the mapping
(`jts_ring_shm.c:751-863`, taken `:956`, released `:1138`); a second writer gets `-EBUSY` +
`event=jts_ring.writer.busy … reason=writer_lock_held`, surfaced verbatim by `pcm_jts_ring.c:300-310`.
`[M]` Heartbeat timeout **2 s** (`jts_ring_shm.h:165`); the writer stamps on attach, every publish entry,
and every full-wait tick; the reader stamps on attach and **every consume call, filled or empty**
(`jts_ring_shm.c:1229-1260`). `[M]`

**Restart hazard already flagged in-tree:** a ring-writing unit's `RestartSec` must exceed 2 s or a fast
respawn races its own frozen heartbeat into an avoidable `-EBUSY`; `jasper-camilla` at `RestartSec=2`
*"sits ON the boundary"* (`jts_ring_shm.h:115-118`). `[M]` **`jasper-snapclient.service` has
`RestartSec=2s`** (`deploy/systemd/jasper-snapclient.service`). `[M]` If snapclient becomes a ring writer,
it inherits that boundary condition.

#### Test-coverage gap

`make test` compiles `test_ring_core.c + jts_ring_shm.c` **only**; `pcm_jts_ring.c` is never compiled by
the test target (`Makefile:51-55` vs `:76-77`). `[M]` So `jts_ring_set_hw_constraints`, `jts_ring_transfer`,
`jts_ring_drain`, and both `poll_revents` are **untested at the C level.** The shared pointer/publish cores
in the header *are* well covered (12+ named tests). This matters because a new consumer exercises exactly
the untested half.

#### The one existing record of a rate controller against a ring

`jts_ring_shm.h:72-89` — the reason `MAX_SLOTS` was raised 4→16 on 2026-07-02, verbatim `[M]`:

> *"CamillaDSP's playback BufferManager negotiates buffer = next_pow2(max(3\*chunksize, 4\*min_period)) and
> then drives its rate controller toward `target_level` frames of device delay. … At n_slots=4 the buffer
> was 512 frames — smaller than both camilla's negotiated 1024 and its target_level (1536), so the rate
> controller **chased an unreachable target, wound up, and drove the writer full (full_waits ~= every
> publish) into stall/underrun flapping.**"*

This is a **playback-side** record (CamillaDSP writing *into* a ring), not capture-side. It is the closest
thing in-tree to a measured rate-controller-vs-ring interaction, and its lesson is precise: **a PI setpoint
that exceeds the ring's total capacity causes integrator wind-up and writer starvation.** `[M]`

---

### 1.5 snapclient 0.31.0 — the sync mechanics

**Version pin, and it is NOT pinned.** Two install paths, both unversioned `apt install`:
`jasper/multiroom/provision.py:205-213` (lazy, on grouping opt-in) and `deploy/install.sh:868-878`
(eager, `streambox` profile only). `[M]` The repo *records* Trixie's version as **0.31.0**
(`provision.py:51-53`, corroborated `docs/multiroom-pairing-reliability-plan.md:39`,
`docs/dumb-endpoint-bringup.md:600`, `jasper/multiroom/reconcile.py:515`). `[M]`
**A Trixie point-release bump would land silently on the next grouping opt-in.** Flagging; not in scope.

**Delay is measured, and it feeds the sync algorithm directly.**
`client/player/alsa_player.cpp:538-560` — `getAvailDelay()` calls `snd_pcm_avail_delay()`, falling back to
`snd_pcm_avail()` + `snd_pcm_delay()`. `[M]` `:632-641`:

```cpp
chronos::usec delay(static_cast<chronos::usec::rep>(1000 * static_cast<double>(framesDelay) / format.msRate()));
...
if (stream_->getPlayerChunk(buffer_.data(), delay, framesAvail))
```

**The device's reported delay becomes `outputBufferDacTime`.** `[M]`

**How it is used** — `client/stream.cpp:379-381` `[M]`:

```cpp
cs::usec age = std::chrono::duration_cast<cs::usec>(TimeProvider::serverNow()
    - getNextPlayerChunk(outputBuffer, frames, framesCorrection)
    - bufferMs_.load() + outputBufferDacTime);
```

snapclient drives `age → 0`. A device that **under-reports** its true time-to-DAC by `D` makes the audio
actually emerge `D` **late** — a *fixed offset*, not drift. `[I]` — direct from the equation.

**The hard gate on a dishonest delay.** `client/stream.cpp:260-266` `[M]`:

```cpp
if (outputBufferDacTime > bufferMs_.load())
{
    LOG(INFO, LOG_TAG) << "outputBufferDacTime > bufferMs: " ...
    return false;   // no chunk is output at all
}
```

Our `bufferMs_` is the server's `--stream.buffer`, default **400 ms**
(`jasper/multiroom/config.py:72-74`; `reconcile.py:467-487`). `[M]` The ring's maximum reportable delay is
**8.0 ms**. So this gate cannot trip on the ring. `[I]` It is nonetheless the documented failure signature
of dishonest delay in the wild `[C]` — e.g. *"outputBufferDacTime > bufferMs: 1003 > 1000"* reports.

**Sync algorithm** (`client/stream.cpp:384-427`) `[M]`:

| Layer | Trigger | Action |
|---|---|---|
| hard sync | `buffer_` full ∧ \|median\| > **2 ms** ∧ \|age\| > 500 µs | skip / insert |
| hard sync | `shortBuffer_` full ∧ \|shortMedian\| > **5 ms** ∧ \|age\| > 500 µs | skip / insert |
| hard sync | `miniBuffer_` full ∧ \|median\| > **50 ms** ∧ \|age\| > 500 µs | skip / insert |
| hard sync | \|age\| > **500 ms** | skip / insert |
| soft sync | `shortBuffer_` full, `shortMedian` beyond `kCorrectionBegin` = **100 µs** | `setRealSampleRate(rate × r)` |

**Soft-sync authority: `rate = 1.0 ∓ std::min(x, 0.0005)` → ±500 ppm maximum** (`stream.cpp:414-425`).
`[M]` That is **10× smaller** than CamillaDSP's ±5000 ppm clamp. Typical crystal drift between two Pis is
tens of ppm, so ±500 ppm has ample authority for drift `[I]` — but it is not a wide-range recovery tool.

**Soft sync is frame drop/duplicate, not resampling.** `setRealSampleRate` computes
`correctAfterXFrames_ = round((r)/(r−1))` (`stream.cpp:84`); `getNextPlayerChunk(out, frames,
framesCorrection)` reads `frames ± correction` and distributes the drop/repeat across slices
(`stream.cpp` `getNextPlayerChunk` overload). `[M]` **CPU cost is negligible** — a memcpy-shaped
rearrangement, no filter. The `Resampler` member (`stream.cpp:72`) exists for *server-rate ≠ device-rate*
format conversion, which does not apply here (both 48 kHz). `[M/I]`

**ALSA negotiation defaults.** `BUFFER_TIME = 80 ms`, `PERIODS = 4`, `MIN_PERIODS = 3`
(`alsa_player.cpp:41-43`). `[M]` It requests `period_time = 80000/4 = 20000 µs`, but first clamps to the
device's reported `[min, max]` period time and then uses `_near` (`:378-406`); `buffer_time` likewise via
`set_buffer_time_near` (`:409-421`). Against the ring's single-valued constraints, period_time would clamp
20000 → 2667 µs (logging *"Period time too large, changing from 20000 to 2667"*) and buffer_time would
resolve to 5333 µs. `[I]` — this is a prediction from reading both sides; **it is exactly what the spike
must confirm.**

`snd_pcm_sw_params_set_avail_min(frames_)` and `set_start_threshold(frames_)` where `frames_` = negotiated
period (`:443-444`), then `snd_pcm_wait(handle_, 100)` per iteration (`:594`), then it writes
**`framesAvail` frames** — whatever the device says is free (`:641-648`). `[M]`

**The known upstream hazard against plugin PCMs.** `alsa_player.cpp:448-452` `[M]`:

```cpp
if (snd_pcm_state(handle_) == SND_PCM_STATE_PREPARED)
{
    if ((err = snd_pcm_start(handle_)) < 0)
        LOG(DEBUG, LOG_TAG) << "Failed to start PCM: " << snd_strerror(err) << "\n";
}
```

snapclient **explicitly starts a playback stream with zero frames written.** This exact block is what
[snapcast#1154](https://github.com/badaix/snapcast/issues/1154) — *"ALSA playback fails when outputting to
dshares since v0.23.0"*, **still open** — blames for `snd_pcm_avail_delay failed: File descriptor in bad
state (-77)` against a `dshare` plugin PCM; the reporter restores playback by deleting those lines. `[C]`
It is still present verbatim at v0.31.0. `[M]`

Our `jts_ring_start` is trivial — `arm_timer(p); return 0;` (`pcm_jts_ring.c:326-330`) — so the plugin
itself has no bad-state path here. `[M]` Whether alsa-lib's generic ioplug layer produces the same
`-EBADFD` shape is **not determinable from this repo** (alsa-lib is not vendored). `[N]` **This is a
spike item, not a prediction.**

Related in-family `[C]`: [snapcast#855](https://github.com/snapcast/snapcast/issues/855) (dropouts with
`pMiniBuffer->full`), [snapcast#755](https://github.com/snapcast/snapcast/issues/755) (Pi buffer-size
dropouts). Community reports of snapclient→CamillaDSP note that *"when using a loopback as source with
CamillaDSP in a snapclient, synchronisation becomes difficult"* `[C]`
(<https://github.com/snapcast/snapcast/discussions/1230>). **No report of snapclient writing into an ALSA
ioplug/external plugin was found** — searched snapcast issues/discussions and general web. `[N]`

**`--latency` is the fixed-offset compensator.** Upstream: *"the delay introduced by the soundcard and amp
or other stuff after snapclient's output"* `[C]`
(<https://github.com/snapcast/snapcast/discussions/743>). The repo already relies on this concept
(`jasper/multiroom/reconcile.py:186-196`: *"the fixed CamillaDSP pipeline latency is nulled by snapclient
`--latency`"*). `[M]` **But `DEFAULT_CLIENT_LATENCY_MS = 0`** (`jasper/multiroom/config.py:80`). `[M]`
So the compensation knob exists and ships **unset** — the "nulled" claim is an intent, not a configured
value.

---

### 1.6 The `snd_pcm_delay`-lies record, and how the ring differs

`docs/HANDOFF-airplay.md:1327-1342`, the five-clock table, verbatim on clock D `[M]`:

> *"shairport reads `snd_pcm_delay()` and assumes it's measuring DAC latency (clock E). What it actually
> returns is the snd-aloop ring fill (a function of writes − reads, which depends on clock D and
> CamillaDSP's drain rate). The fill **looks** like drift to shairport but is decoupled from the real
> audio clock."*

Consequence measured (`:925-936`): shairport crossed its 50 ms `resync_threshold` and *"drops ~6,600 source
frames + injects up to 250 ms of zeros"*, presenting as `+50 ms` / `−485 ms` sync errors. `[M]`

**The repo already anticipated the ring's difference** — `docs/HANDOFF-airplay.md:98-107` `[M]`:

> *"The ring ioplug reports an honest occupancy-derived delay instead, so on an armed box the delay signal
> has different dynamics."*

**Assessment.** The ring's `.delay` is *the same class of quantity* as aloop's — occupancy, not
time-to-DAC. What differs is **magnitude and dynamics** `[I, from the two `[M]` sources]`:

- aloop: a deep ring whose fill **ramps** with real crystal drift over minutes, so the reported value moves
  tens of milliseconds and crosses a threshold.
- `jts_ring` @ 2×128: bounded at **8.0 ms**, and pinned near capacity by the blocking-writer handshake
  (`60-jts-ring.conf:47-48`: *"the blocking-writer handshake pins fill at capacity"*). `[M]` Its variation
  is a **one-slot sawtooth = 128 frames = 2.667 ms peak-to-peak**, because the reader consumes exactly one
  slot per chunk. `[I]`

So the ring under-reports by a **large but near-constant** `D` (the whole downstream chain: CamillaDSP
chunk + queue + outputd + DAC), with a small bounded ripple. A constant offset is `--latency`'s job. The
ripple is the risk: **2.667 ms peak-to-peak sits directly between snapclient's 2 ms long-median and 5 ms
short-median hard-sync thresholds.** Medians should smooth a symmetric sawtooth, but "should" is exactly
what a spike is for. `[I]`

Note also: **the ring's sawtooth amplitude is one period regardless of `n_slots`** — deepening the ring
raises the mean and the headroom without widening the ripple. `[I]`

---

### 1.7 The current bonded-follower chain — and the premise correction

**Chain as built** (`jasper/multiroom/follower_config.py:18-22`, `reconcile.py`, subagent trace) `[M]`:

```
leader: fanin → CamillaDSP#1 (File sink, rate_adjust FALSE) → /run/jasper-snapserver/snapfifo
      → snapserver --stream.source pipe://…&sampleformat=48000:16:2&codec=flac --stream.buffer 400
      → LAN
follower: snapclient --host <leader>.local --latency 0 --soundcard hw:Loopback,0,6 --player alsa
      → snd-aloop pair 6 → CamillaDSP#1 captures hw:Loopback,1,6 (S16_LE, raw hw:, no plug)
      → Layer A (2→N split, per-driver crossover/limiter/tweeter HP)
      → jts_ring_active_playback → jasper-outputd → DAC(s)
```

Constants: `GROUPING_LOOPBACK_PLAYBACK = "hw:Loopback,0,6"`, `GROUPING_LOOPBACK_CAPTURE =
"hw:Loopback,1,6"`, `GROUPING_LOOPBACK_CAPTURE_FORMAT = "S16_LE"`
(`jasper/multiroom/reconcile.py:214-225`). `[M]`

#### ⚠ The premise correction

The brief asserts the follower "runs `rate_adjust: true`". **On a ring-armed box it does not.** Proof
chain, four `[M]` links:

1. `jasper/active_speaker/baseline_profile.py:1839` —
   `devices = active_emit_devices(resolved_playback_device, topology=topology)`.
2. `jasper/active_speaker/camilla_yaml.py:468-484` — `active_emit_devices` branches **on the playback
   device**: `if playback_device not in RING_PCM_DEVICES:` → `enable_rate_adjust =
   DEFAULT_ACTIVE_ENABLE_RATE_ADJUST` (**True**); else → `enable_rate_adjust =
   RING_CAMILLA_ENABLE_RATE_ADJUST` (**False**), `chunksize=128`, `target_level=128`, `queuelimit=1`.
3. `jasper/output_topology.py:1895-1918` — for any DAC profile with `supports_active_outputd_lane` and
   `active_outputd_lane_channels`, `playback_device = RING_ACTIVE_PLAYBACK_DEVICE`, **"unconditionally —
   there is no second legal endpoint to choose between"**; the former `ring_active_endpoint_armed()`
   fallback branch was deleted on purpose.
4. `jasper/active_speaker/baseline_profile.py:2388-2400` — the follower's driver-domain emit forwards
   `enable_rate_adjust=devices.enable_rate_adjust` and overrides **only** `capture_device` /
   `capture_format`.

**Therefore, on a production ring-armed active follower today:**
capture `hw:Loopback,1,6` (aloop, S16_LE) · playback `jts_ring_active_playback` ·
**`enable_rate_adjust: false`** · `chunksize: 128` · `target_level: 128` · `queuelimit: 1` · no resampler.

**Two consequences.**

**(a) The guard test pins a fixture that no longer represents production.**
`tests/test_multiroom_follower_config.py:942-949`, `_follower_driver_domain_devices()` hardcodes
`playback_device="hw:CARD=DAC8x,DEV=0"` — a **DAC**, so it takes the non-ring branch. `[M]` The three
clock-seam tests then assert `enable_rate_adjust is True` and `chunksize >= 1024` (`:953-968`) — both of
which a ring-armed box now contradicts. The header comment justifying `>= 1024` says *"512 → EPIPE
underruns on a Pi"*; production now emits **128** on that same loopback capture. `[M]` I have **no field
evidence** that 128 misbehaves there — flagging the contradiction, not claiming a failure. `[I]`

**(b) The drift question is already live, unanswered, on the shipped aloop path.** With CamillaDSP's
rate-adjust off, nothing in CamillaDSP tracks the aloop-write-clock ↔ DAC-clock relationship on a
ring-armed follower. snapclient's ±500 ppm soft sync is already the sole tracker **today**. That is
Architecture B — arrived at by accident of the playback-device refactor rather than by design, and never
measured in that configuration.

**The S0-sync bench does not cover this.** `docs/HANDOFF-distributed-active.md:813-892` (2026-06-20)
measured `snapclient → snd-aloop → crossover-only CamillaDSP → **real DAC**`, with rate_adjust **on** and
`Capture device supports rate adjust` in the log, buffer_level holding 1024, `rate_adjust` 0.99980–1.00007,
0 xruns over ~0.65 h on both a Pi 5 and jts4. `[M]` **That topology (Camilla → real DAC) is not what
ships now** (Camilla → active ring → outputd → DAC). The bench's own stated gaps remain open: the ≥24 h
xrun soak was never run, and the acoustic p99 < 5 ms was deferred (`:884-892`). `[M]`

#### Why the FIFO path cannot serve the active follower

Recorded, three places `[M]`:
- `jasper/multiroom/reconcile.py:184-199` — *"An ACTIVE (multi-driver) follower cannot use the
  dumb-follower `dac_content` FIFO path — its CamillaDSP must run Layer A (the crossover) in the bonded
  audio path."*
- `jasper/multiroom/follower_config.py:8-27` — the safety argument: *"sending the full-range program to a
  tweeter would destroy it."*
- `jasper/multiroom/reconcile.py:517-525` — *"the `snd_pcm_delay` trap is avoided not by dodging snd-aloop
  but by CamillaDSP owning the clock + `--latency` nulling the fixed pipeline latency."*

**Note the recorded reason is positive** ("Camilla must be in the bonded path and needs an ALSA capture
with a trackable clock"), **not** that a FIFO is technically impossible for CamillaDSP — it has a File
capture backend. No doc in this worktree evaluates "FIFO → CamillaDSP capture" for the active follower and
rejects it. `[N]` — searched `jasper/multiroom/*.py`, `docs/HANDOFF-multiroom.md`,
`docs/HANDOFF-distributed-active.md`, `docs/dumb-endpoint-bringup.md`,
`rust/jasper-outputd/src/dac_content.rs`, `deploy/systemd/jasper-snapclient.service`. Given link 3's
premise ("CamillaDSP owning the clock") is now false on a ring-armed box, **that rejection deserves
re-derivation rather than inheritance.**

---

## 2. Candidate architectures

Common to all: capture rate == playback rate == 48 kHz; JTS emits no resampler anywhere
(`jasper/sound/camilla_yaml.py`, `jasper/active_speaker/camilla_yaml.py` — grep for `resampler:` in
emitters returns nothing; `DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE` was deleted 2026-07-15,
`docs/HANDOFF-audio-latency-foundation.md:296-300`). `[M]`

---

### Architecture A — keep `enable_rate_adjust: true`, CamillaDSP captures the ring

**What CamillaDSP needs from a capture device to rate-track:** an ALSA PCM element named
`PCM Rate Shift 100000` (aloop) or `Capture Pitch 1000000` (UAC2 gadget) at the device's own
card/device/subdevice — which requires `snd_pcm_info.card >= 0`.

**Evidence FOR:** none found. `[N]`

**Evidence AGAINST — dispositive:**
- ioplug hard-codes `info->card = -1` (`alsa-lib/src/pcm/pcm_ioplug.c:91-95`). `[M]`
- CamillaDSP skips HCtl creation entirely when `card < 0`, with a comment naming *"Virtual devices such as
  pcm plugins"* (`device.rs:767-769`, `:512-514`). `[M]`
- With both elements `None` and no resampler, `SetSpeed` matches no branch and **does nothing, silently**
  (`device.rs:896-917` — no final `else`). `[M]`
- The startup line `Capture device supports rate adjust` would **not** print (`device.rs:828-829`). `[M]`
- v4.1.3 README: with no tuning path and no async resampler, *"Rate adjust requests cannot be applied."*
  `[C]`

**Failure mode if shipped:** not oscillation — **silent inertness with a misleading indicator.** The PI
controller still runs, still integrates, and still publishes `capture_status.rate_adjust`
(`device.rs:972`), so `/state` and the websocket show a live-looking correction that is never applied.
Drift accumulates exactly as with rate_adjust off, but the observability surface asserts it is handled.
That is strictly worse than off. `[I]`

**Sub-variant A′ — add an async resampler to give tier 3 something to actuate.** Mechanically it would
work (`device.rs:906-913`). Against it: it is a 1:1 resample whose only purpose is drift, it reintroduces
the exact `rate_adjust` + async-resampler shape the repo's contract predicate exists to forbid on
aloop-class captures (§1.2), it costs CPU on a Zero 2 W, and CamillaDSP would emit *"Async resampler is
used but not needed…"* only if a tuning element were present — here it would be silent. `[M/I]`
**No evidence supports it; listing for completeness.**

**Verdict:** ruled out by construction, not by risk appetite.

---

### Architecture B — `rate_adjust` off, snapclient sole tracker via back-pressure

**The claim:** the SHM ring has no clock; CamillaDSP's loop is paced by its blocking DAC-side write;
back-pressure propagates the DAC's consumption rate through CamillaDSP to the ring writer; snapclient's
own ±500 ppm soft sync then absorbs leader↔follower drift.

**Evidence FOR:**
- **Back-pressure is genuine.** `.pointer` returns `appl_frames − in_flight`; `in_flight` is derived from
  `occupancy = write_seq − read_seq`; `read_seq` is Released **only** by the real reader on a successful
  consume (`pcm_jts_ring.c:386-401`; `jts_ring_shm.c:1101-1105`, `:1297-1299`). Pinned by
  `test_occupancy_tracks_reader_drain` (`test_ring_core.c:1867-1923`). `[M]` The timerfd paces *polling
  only* and never touches `appl_frames`/`write_seq`/`read_seq`. `[M]`
- **The POLLOUT gate is honest.** Withheld iff full ∧ reader live (`pcm_jts_ring.c:881-904`;
  `jts_ring_shm.c:1112-1120`), pinned by `test_can_accept_semantics`. snapclient blocks in
  `snd_pcm_wait()` (`alsa_player.cpp:594`), which is the standard consumer of that gate. `[M]`
- **The current Ring B reader is strictly DAC-paced** — `ShmRingSource::read_period` consumes exactly one
  slot per DAC period (`rust/jasper-outputd/src/shm_ring_source.rs:12-16, 204-205`). `[M]`
- **The delay-honesty gate cannot trip.** Ring max 8.0 ms vs `bufferMs_` 400 ms. `[I]`
- **snapclient's soft sync is nearly free** — frame drop/duplicate, no filter (`stream.cpp:84`,
  `getNextPlayerChunk`). Good for the Zero 2 W budget. `[M]`
- **It is already the de-facto shipped state** on ring-armed followers (§1.7), so Phase 1 would be
  *changing the transport*, not *introducing* this clock arrangement. `[M]`
- **Precedent:** the ring's paired CamillaDSP config for the solo box is already
  `chunk 128 / target 128 / queue 1 / rate_adjust off` and is hardware-validated
  (`jasper/fanin_coupling.py:94-97`; `jasper/sound/camilla_yaml.py:529-573`). `[M]`

**Evidence AGAINST / requirements not met:**

1. **`.delay` under-reports by the entire downstream chain.** `pcm_jts_ring.c:440-460` returns ring fill
   only; the comment explicitly scopes the intended consumer: *"`.delay` is only consulted by a LIVE
   pacer's rate controller (CamillaDSP)"*. `[M]` **Architecture B falsifies that assumption** by making
   snapclient — which reads the value as time-to-DAC — a consumer. The plugin's own contract note says to
   revisit if a new consumer appears. Impact is a **fixed late offset**, whose correct remedy is
   `--latency`, which currently ships at **0** (`jasper/multiroom/config.py:80`). `[M]`
2. **2.667 ms of delay ripple against a 2 ms hard-sync threshold.** One-slot sawtooth vs
   `buffer_` median > 2 ms (`stream.cpp:384`). Medians should absorb a symmetric sawtooth; unproven. `[I]`
3. **5.33 ms is a very thin scheduling margin on a Zero 2 W.** snapclient's own ALSA default asks for
   80 ms (`alsa_player.cpp:41`). If snapclient is descheduled longer than 5.33 ms, the ring empties.
   **And the ring does not fabricate silence for a live-but-slow writer** — `capture_service_tick` arms
   silence only when `!writer_live` (heartbeat-dead, >2 s) *and* the ring is really empty
   (`pcm_jts_ring.c:601-637`). `[M]` So a momentary snapclient stall gives CamillaDSP a short/zero capture
   read, not paced silence.
4. **The >2 s dead-reader cliff is a silent-corruption mode.** If CamillaDSP stops calling consume for
   >2 s, `in_flight` collapses to `stage` only, `avail` reads ~full forever, POLLOUT is always granted, and
   the writer free-runs while `publish` overwrites the oldest slot (`jts_ring_shm.h:566-604`;
   `jts_ring_shm.c:1062-1067`). `[M]` **All back-pressure vanishes with no error to snapclient**, and
   `.transfer` still returns full acceptance (`pcm_jts_ring.c:433`). snapclient would then have no clock
   reference at all, only its own `age` term — whose `delay` input has also gone stale-honest. `[M/I]`
5. **Drops are structurally invisible.** `.transfer` always returns `size`; the publish result is
   discarded; no `-EPIPE` is ever raised; `full_waits` surfaces **only** in the close-time `SNDERR`
   (`pcm_jts_ring.c:428`, `:433`, `:536-539`; `[N]` on EPIPE). `[M]` A dropping ring looks identical to a
   healthy one from snapclient's side.
6. **The untested half of the plugin is exactly the half a new writer exercises** — `make test` never
   compiles `pcm_jts_ring.c` (`Makefile:51-55` vs `:76-77`). `[M]`
7. **`RestartSec=2s` on `jasper-snapclient.service` sits on the 2 s heartbeat boundary**
   (`jts_ring_shm.h:115-118`). A crash-restart could race its own frozen heartbeat into `-EBUSY`. `[M]`
8. **snapclient's explicit `snd_pcm_start()` on an empty buffer is a known plugin-PCM hazard**
   (`alsa_player.cpp:448-452`; snapcast#1154, open). `[M/C]`
9. **Negotiation is unproven.** Six single-valued constraints (§1.4) vs snapclient's
   `set_period_time_near` / `set_buffer_time_near` flow. Predicted to succeed `[I]`; unobserved.

**Requirements Architecture B would impose:** honest-enough (constant) delay + a non-zero `--latency` to
null the offset; a scheduling margin larger than one ring depth; drop observability that reaches `/state`;
and a decision about the >2 s cliff.

---

### Architecture C — variants the evidence actually suggests

**C1 — Deeper grouping ring as the jitter buffer.** A grouping-specific PCM (e.g. `jts_ring_grouping`)
declaring `n_slots` well above 2, up to the existing ceiling of 16 → 2048 frames = **42.7 ms**, close to
snapclient's own 80 ms default.
*For:* geometry is already per-PCM-instance configurable with a clean `-EINVAL` outside 2..16
(`pcm_jts_ring.c:1094-1105`); `MAX_SLOTS=16` exists and `test_deep_ring_16_slots` covers it
(`test_ring_core.c:1824`); the ripple stays one period regardless of depth `[I]`; it directly attacks
Architecture B's risks 2 and 3; on a synced chain the added fixed latency is exactly what `--latency`
nulls. `[M/I]`
*Against:* raises steady-state latency on a box whose whole ring program was tuned to 5.3 ms
(`60-jts-ring.conf:43-92`); needs a second ring geometry in the tree, which is a single-source-of-truth
question, not just a number. `[M]`
*Note:* `jts_ring_shm.h:72-89` is a **measured** precedent that ring depth vs a controller setpoint is the
axis that mattered before. `[M]`

**C2 — Set `--latency` to the measured pipeline offset.** Independent of transport, and required by both
B and C1 for inter-speaker sync. Today `DEFAULT_CLIENT_LATENCY_MS = 0` while the code comments claim the
latency "is nulled by `--latency`" (`jasper/multiroom/config.py:80` vs `reconcile.py:186-196`). `[M]`
**This is a live inconsistency on the *current* aloop path, not something Phase 1 introduces.**

**C3 — Re-derive the FIFO option for the active follower.** The recorded rejection rests on "the active
follower needs the loopback's clock for CamillaDSP's `rate_adjust` to track"
(`reconcile.py:191-193`). `[M]` On a ring-armed box `rate_adjust` is already **off**, so that premise no
longer holds, and CamillaDSP does have a File capture backend. No document evaluates and rejects
FIFO→CamillaDSP-capture on its merits. `[N]` **Flagging as an unexamined option whose stated blocker has
expired — not advocating it.** A File capture has no clock at all, which changes the analysis materially
and is beyond what documents settle here.

**C4 — Snapserver-side resampling.** *Speculative; no supporting evidence found.* `[N]` Listed only so
the option space is complete. Snapserver's role here is a pipe reader with a fixed
`sampleformat=48000:16:2` (`reconcile.py:467-476`) `[M]`; nothing indicates it can absorb per-follower
drift.

**C5 — A minimal pacer between snapclient and the ring.** *Speculative.* Adds a process to the
Zero 2 W and a clock domain to a chain whose stated goal is fewer of both. No evidence gathered for or
against. `[N]`

---

## 3. What documents cannot settle — the spikes

**Box constraint.** `jts3` is FORBIDDEN (peer session owns it). Available: `jts.local` (per session
memory: ring-armed, dual Apple DAC, **dummy loads fitted** — the safe box for any full-range mistake) and
`jts4` (Zero 2 W, `streambox` profile, snapclient/snapserver installed eagerly,
`deploy/install.sh:868-878`). `[M]`

**Suggested pairing:** `jts4` = leader (runs `snapserver` + its own snapclient); `jts.local` = the
follower under test. Rationale: the active-follower role requires an active-speaker commission and a ring
arm, which `jts.local` has and `jts4` (a dumb/streambox endpoint) does not. `[I]` **S0 below is
box-agnostic and should run first.**

---

### S0 — Does snapclient negotiate and run against the ioplug at all?
**Cheapest, highest information, gates everything else. No grouping needed.**

Point a snapclient at a `jts_ring_*` playback PCM with a live reader attached, and read its log.

| Signal | PASS | FAIL |
|---|---|---|
| hw_params | `PCM name: …, sample rate: 48000 Hz, channels: 2, buffer time: 5333 us, periods: 2, period time: 2667 us, period frames: 128` | any `Can't set …` exception (`alsa_player.cpp:373-424`) |
| the #1154 shape | **no** `snd_pcm_avail_delay failed: … bad state (-77)` | that line repeating |
| write path | no `Can't write to PCM device` | `-EBADFD` / repeated re-init |
| clamp logs | `Period time too large, changing from 20000 to 2667` (expected, benign) | — |

**Settles:** Architecture B risks 8 and 9, and the snapcast#1154 exposure. **If S0 fails, B and C1 both
die and the memo's whole option space collapses to C3/C5.**

---

### S1 — Is CamillaDSP's rate adjust inert on a ring capture? (confirms §1.1 on metal)
Run a CamillaDSP config with `capture: jts_ring_capture` and `enable_rate_adjust: true`, no resampler,
at `debug` log level.

| Signal | PASS (= inert, as predicted) | FAIL (= prediction wrong) |
|---|---|---|
| startup | `Capture device supports rate adjust` **ABSENT** | line present |
| runtime | `Setting capture loopback speed to …` / `… gadget speed …` / `… async resampler speed …` **all ABSENT** | any present |
| status | `capture_status.rate_adjust` **≠ 1.0 and drifting** while the above are absent | — (this confirms the observability trap) |

**Settles:** Architecture A definitively, on hardware, in under a minute. **Do not use the
`rate_adjust` status field as evidence of function** (`device.rs:897`, `:972`).

---

### S2 — Delay honesty and ripple as a WRITER sees them
Instrument a writer against the ring (or read snapclient's own trace) and log `snd_pcm_delay` at ≥100 Hz
for 10 min under steady playback.

| Signal | PASS | FAIL |
|---|---|---|
| mean | stable within ±1 ms over 10 min (constant ⇒ `--latency`-correctable) | slow ramp (the aloop Pattern-B shape) |
| ripple | peak-to-peak ≤ ~2.7 ms (one slot), as predicted | > 5 ms |
| absolute | ≤ 8.0 ms always (≪ `bufferMs_` 400 ms) | any excursion above 400 ms |

**Settles:** §1.6's ripple prediction, and whether the offset is constant (correctable) or drifting (not).

---

### S3 — snapclient stability against the ring over time (the real Architecture-B gate)
Bonded pair, follower ingress on the ring, ≥2 h, then ≥24 h for durability.

| Signal | PASS | FAIL |
|---|---|---|
| hard syncs | **zero** `pBuffer->full() && (abs(median_) > 2)` / `pShortBuffer…> 5` / `pMiniBuffer…> 50` after the first 60 s settle | recurring |
| the gate | zero `outputBufferDacTime > bufferMs` | any |
| chunk starvation | zero `Failed to get chunk` in steady state | recurring |
| soft sync | `correctAfterXFrames_`-driven rate stays inside ±500 ppm without pinning at the clamp | pinned at ±0.0005 (⇒ authority exhausted, drift unabsorbed) |
| CamillaDSP | zero `Capture read short`, zero `Prepare playback after buffer underrun` | any recurring |
| ring counters | close-time `full_waits` / `drop_no_reader` ≈ 0 (`pcm_jts_ring.c:536-539`) | non-trivial counts |
| Zero 2 W budget | snapclient CPU < ~10 % of one core; RSS ≈ 5 MB (bench baseline, `HANDOFF-distributed-active.md:847`) | above |

**Note the CPU line specifically:** the ring's poll fd is a bare timerfd at ~1500 Hz
(`pcm_jts_ring.c:866-879`), 7.5× snapclient's normal ~200 Hz wake rate at its 80 ms/4 default. Measure it.

**Settles:** Architecture B risks 2, 3, 5; and whether ±500 ppm alone suffices.

---

### S4 — The >2 s dead-reader cliff
Deliberately stall CamillaDSP (SIGSTOP ≥3 s) while snapclient writes; then resume.

| Signal | PASS | FAIL |
|---|---|---|
| during | audible dropout is bounded and snapclient recovers | unbounded drift, or snapclient never resyncs |
| after | `drop_no_reader` incremented **and visible somewhere an operator can see** | counters only in a close-time log nobody reads |
| recovery | back to steady state within one `--stream.buffer` (400 ms) | requires a restart |

**Settles:** Architecture B risk 4 — and whether the free-run mode needs an observability surface before
a *network-fed* writer is allowed on the ring (unlike fan-in, snapclient cannot be assumed co-scheduled
with its reader).

---

### S5 — Does the shipped ring-armed follower already have unabsorbed drift? (validates §1.7(b))
**No code change. Read the config on a currently-bonded ring-armed follower.**

| Signal | Confirms |
|---|---|
| `grep enable_rate_adjust /var/lib/camilladsp/configs/grouping_follower.yml` → `false` | the premise correction is real in the field, not just in the emitters |
| `chunksize: 128` in the same file | the `>= 1024` clock-seam guard is bypassed in production |
| CamillaDSP log lacks `Capture device supports rate adjust` | rate adjust is already off on the aloop path too |
| snapclient hard-sync frequency on the **current** build | the baseline any ring-ingress change must beat |

**Settles:** whether §1.7 is a live production gap or an emitter-path artifact. **Run this first — it is
free and it may reframe Phase 1 entirely.**

---

## 4. Verdict on the conductor hypothesis

**PARTIAL — the mechanism is confirmed, the premise is refuted, and the conclusion is right for a
different reason than the one given.**

**Confirmed.** The SHM ring has no clock of its own, and back-pressure is genuine: the ring's hardware
pointer advances only when the real reader Releases `read_seq`, so the DAC's consumption rate does
propagate through CamillaDSP to the ring writer (`pcm_jts_ring.c:386-401`; `jts_ring_shm.c:1101-1105`,
`:1297-1299`; pinned by `test_occupancy_tracks_reader_drain`). Removing the aloop hop does remove a
kernel-timer clock from the middle of the chain. And CamillaDSP can indeed run the shipped rate_adjust-OFF
ring-capture path, because that is already the certified Ring A geometry
(`jasper/fanin_coupling.py:94-97`).

**Refuted — the load-bearing unknown does not exist as stated.** The brief's premise that the bonded
follower "runs `rate_adjust: true` … as its sole drift absorber" is already false on a ring-armed box.
`enable_rate_adjust` is keyed off the *playback* device (`camilla_yaml.py:468-484`), the playback device
resolves to the ACTIVE ring **unconditionally** (`output_topology.py:1895-1918`), and the follower's emit
forwards that value while overriding only the capture device
(`baseline_profile.py:1839`, `:2388-2400`). Production therefore already ships
`enable_rate_adjust: false`, `chunksize: 128`, `target_level: 128` on the follower — while the clock-seam
guard test asserts `True` and `>= 1024` against a DAC-playback fixture
(`tests/test_multiroom_follower_config.py:942-968`). So "a rate-tracking CamillaDSP against a ring has
never shipped in either direction" is true but beside the point: **a rate-tracking CamillaDSP is not what
the follower runs today either.** Phase 1 changes the ingress transport only.

**Right conclusion, stronger reason.** The hypothesis proposes CamillaDSP run rate_adjust-off on the ring;
that is correct — but not merely because it is the validated Ring A shape. It is correct because
`enable_rate_adjust: true` on any ioplug capture is **inert by construction**: ioplug reports `card = -1`
(`alsa-lib/src/pcm/pcm_ioplug.c:91-95`), CamillaDSP therefore builds no HCtl (`device.rs:767-769`), neither
rate-shift element can be found (`device.rs:780-793`), and with no resampler the `SetSpeed` arm falls
through silently (`device.rs:896-917`). The choice is forced, not merely preferred — and the repo's own
stated second reason for it ("rate_adjust over an snd-aloop-class transport is a documented oscillation
shape") is a mis-attribution: the documented oscillation requires an **async resampler**, which the ring
path does not and cannot have (`camilla_config_contract.py:390-397`).

**The catch is real and is the surviving risk.** snapclient does consume `snd_pcm_delay` as time-to-DAC
(`alsa_player.cpp:538-560`, `:632-641`; `stream.cpp:379-381`), and our ring reports ring fill only — the
same *class* of under-report as the aloop lie, differing in magnitude and dynamics. The plugin's own
`.delay` comment blesses a contract naming CamillaDSP as its only consumer and asks that a new consumer
trigger a revisit (`pcm_jts_ring.c:443-454`); Architecture B is exactly that new consumer. The impact is a
**fixed late offset** (correctable by `--latency`, which ships at 0) plus a **2.667 ms one-slot ripple**
sitting between snapclient's 2 ms and 5 ms hard-sync thresholds.

**What settles the remainder:** S0 (negotiation + the snapcast#1154 shape) gates everything; S5 is free and
may reframe the phase; S2 answers the delay-honesty catch; S3 is the real Architecture-B gate; S4 covers
the >2 s cliff. S1 confirms §1.1 on metal in under a minute.

**Two open items outside this memo's scope but on its evidence path:** the `~194 ms` rate_adjust-on
Ring A+B measurement that is the sole empirical justification for `RING_CAMILLA_ENABLE_RATE_ADJUST = False`
survives only as a code comment (`jasper/audio_runtime_plan.py:1671-1677`) — `captures/` is absent from
this worktree and the one doc pointer (`docs/HANDOFF-usb-latency-measurement.md:159` → HANDOFF-usb-low-latency
"conservation law") is **stale: that section does not exist**. And the S0-sync bench's own acceptance bar
(acoustic p99 < 5 ms, ≥24 h soak) was never met — ~0.65 h of telemetry was accepted instead
(`docs/HANDOFF-distributed-active.md:884-892`).

---

## 5. Constraints this memo respects

- **1 GB Pi budget / Zero 2 W member.** jts4 is a Zero 2 W with **512 MB** (`QUICKSTART.md:20`;
  `docs/dumb-endpoint-bringup.md:25`). Measured steady state under the active-follower load:
  CamillaDSP ≈ 5.5 MB Pss, snapclient ≈ 5 MB, load < 1.1, no throttling
  (`docs/HANDOFF-distributed-active.md:847-849`). `[M]` Every option above is evaluated for CPU: snapclient's
  soft sync is frame drop/duplicate (free); an async resampler (A′) and any added pacer (C5) are not.
  The ring's ~1500 Hz timerfd wake rate is called out as a measurable cost in S3.
- **No PipeWire / `module-echo-cancel` re-architecture.** Not proposed anywhere. Every candidate stays
  inside the existing ALSA + CamillaDSP + outputd topology.
- **Forward-only, no compat shims.** No option proposes keeping the aloop path alongside the ring, or a
  runtime switch between them. C1 proposes a *geometry* parameter on an existing per-PCM-configurable
  axis, not a second code path.
- **Single source of truth.** C1's second ring geometry is explicitly flagged as an SSOT question rather
  than a free parameter. §1.7's finding is reported as a **divergence between the emitter path and its
  guard test** — one of the two must move; this memo does not choose which.
- **Safety.** Any spike touching an active follower's graph risks a full-range feed to a tweeter, which
  is why `jts.local` (dummy loads fitted) is proposed over any speaker with live drivers, and why the
  emit gate that refuses an unprotected tweeter
  (`jasper/multiroom/follower_config.py:198-224`) must stay in the path for every spike.

---

*Research memo — no repo files were modified. Read-only against worktree `6e569e8dc` throughout;
upstream sources fetched at their pinned tags (CamillaDSP `v4.1.3`, snapcast `v0.31.0`, alsa-lib master).*
