# XVF3800 chip-side AEC — final test plan

A revised version of the user-proposed parameter sweep, scoped to
the chip-side knobs that actually have a chance of changing the
outcome. The goal is to **conclusively** decide whether chip-side
AEC can deliver useful attenuation in our external-DAC topology
before we commit any further effort to (or away from) software AEC.

This document is a plan, not an execution. Reading time ~5 min;
execution time on the Pi ~2 hours.

---

## What's already known

From the prior investigation (see `docs/HANDOFF-aec.md`):

- **Best chip-side attenuation measured: ≤ −2 dB sustained.** That's
  with `AUDIO_MGR_SYS_DELAY` swept −64 to +256 (the chip's silently-
  enforced range), `AEC_FAR_EXTGAIN` swept −40 → +20 dB, after
  fixing the UAC2 PCM volume mirror.
- **`SHF_BYPASS` is not a clean AEC disable.** Per the XMOS
  Programming Guide v2.0.0 §4.1, it's a timing-stress mode that
  switches between "raw amplified mics" (`=1`) and "full SHF
  pipeline incl. AEC + NS + BF + AGC + NLES" (`=0`). Our prior
  "bypass vs AEC ON" measurements were actually "raw single-mic
  vs full 4-mic-beamformed pipeline" — that ~+6 dB beamformer
  sum-gain swamped any AEC contribution we were trying to measure.
- **The AEC adaptive filter HAS adapted in the past** (filter
  coefficient dump showed RMS 0.224 with peaks at taps 2 and 243),
  but with peak magnitudes >1.0 indicating LMS runaway from when
  the volume mirror was broken. After fix + reboot, the filter
  dumped as all zeros — almost certainly because each iteration of
  our previous sweep was changing `SYS_DELAY` and resetting
  adaptation before the filter could converge.
- **The XMOS documentation explicitly says the AEC pipeline assumes
  the chip drives the speaker** via its own DAC (`I2S_DAC_DSP_ENABLE`
  description: *"If enabled (1), use the I2S signal as input to the
  audio pipeline, even in the UA configuration"*). Our topology
  (external USB DAC drives the speaker) is the unmarked trail.

The **architectural mismatch** is the most likely root cause. But
the chip exposes ~20 untested parameters that affect the AEC and
post-AEC echo suppression. Until those are tested, the door isn't
fully closed.

---

## What we're testing for

A single decision: **can chip-side AEC deliver ≥ −15 dB sustained
attenuation in our topology under any parameter combination?**

| Outcome | Decision |
|---|---|
| ≥ −15 dB at any combination | Ship it. Software AEC becomes redundant; tear down. |
| −5 to −15 dB | Compare against software AEC (~ −8 dB peak measured). Pick whichever is more reliable / lower RAM. |
| < −5 dB | Chip-side is conclusively dead in this topology. Choose software AEC vs no-AEC default. |

---

## Methodology corrections from the previous test

These are the things the prior test got wrong; the revised plan
fixes them.

### 1. Use a real AEC-on/AEC-off comparison

Instead of `SHF_BYPASS=1` vs `SHF_BYPASS=0` (which compares "no
pipeline at all" vs "full pipeline" — beamformer dominates), the
correct comparison keeps everything else equal and toggles only the
AEC reference signal:

- **AEC effectively-OFF run**: `SHF_BYPASS=0`, `AEC_FAR_EXTGAIN=-120`
  (reference signal nullified — AEC has nothing to subtract)
- **AEC ON run**: `SHF_BYPASS=0`, `AEC_FAR_EXTGAIN=0` (or whatever
  the EXTGAIN is mirroring from PCM volume)

Difference between the two runs IS the linear AEC's contribution.
Beamformer, NS, AGC are identical in both → cancel out.

### 2. Long-soak instead of multi-iteration sweep

The prior test changed `SYS_DELAY` 17 times, with 5 sec of audio
between changes. Each change resets the AEC's adaptive filter; 5 sec
isn't enough time to re-converge. Filter dumps after the test
showed all-zero coefficients — consistent with "kept resetting
before convergence."

Revised: **single fixed parameter set per run, 60 sec of sustained
audio, filter coefficient dump pre and post.** This actually
measures whether the AEC ever fully adapts, and whether the
converged filter has structured (good) or runaway (bad) coefficients.

### 3. Disable AGC during measurement

AGC normalizes the post-pipeline signal to a target level, hiding
real attenuation. `PP_AGCONOFF=0` for the duration of each
measurement; restore after.

### 4. Volume safety — never raise above current

Read current `master_gain` first; clamp the test volume to
`min(target, current)`. Always restore in `finally`. (Same
principle as `jasper-aec-tune` — already burned by this once.)

### 5. Always SHF_BYPASS=0 during real measurements

Bypass mode produces unrelated output (raw mics with poll-loop
timing stress). Never use it in the actual measurements; only as
a sanity check that the rest of the chain is alive.

---

## Phase-ordered test matrix

Runs in priority order. **Stop early** if any phase finds ≥ −15 dB —
no point continuing, ship that combination.

Each phase: hold all other parameters at their current state,
toggle the phase-under-test parameters, run a 60-sec sustained
sweep, measure mic RMS, dump filter coefficients, compute
`attenuation_dB = 20*log10(rms_AEC_ON / rms_AEC_OFF)`.

### Phase 1 — Long-soak baseline at default parameters (10 min)

Establishes whether the AEC ever converges with adequate time at
the existing tuned `AUDIO_MGR_SYS_DELAY` value, with all other
parameters at default. This is the test we should have run before
declaring chip-side dead.

| Param | Value |
|---|---|
| `SHF_BYPASS` | 0 (full pipeline) |
| `PP_AGCONOFF` | 0 (AGC off — clean measurement) |
| `AUDIO_MGR_SYS_DELAY` | best from prior tune (96 samples) |
| `AEC_FAR_EXTGAIN` | 0 dB (UAC2 PCM at unity) |
| All others | factory default |

**Procedure**: AEC-OFF run (`EXTGAIN=-120`), 60 sec sustained sweep,
measure. AEC-ON run (`EXTGAIN=0`), 60 sec, measure. Filter dump
pre/post on the AEC-ON run.

**Decision gate**: if attenuation ≥ −10 dB AND filter dump shows
structured coefficients (peak <1.0, identifiable acoustic-delay
peak, RMS substantial), AEC works at default — go to phase 7
(scaling/refinement). Otherwise continue.

### Phase 2 — Post-AEC echo suppressor (`PP_ECHOONOFF` family) (~30 min)

This is the highest-priority untested area. The post-AEC suppressor
operates on level/spectrum analysis, **independent of the adaptive
filter's convergence state**. It's the strongest candidate for
delivering attenuation when the linear AEC is failing.

Test grid (each cell is a 60-sec long-soak run):

| `PP_ECHOONOFF` | `PP_GAMMA_E` | `PP_GAMMA_ETAIL` | `PP_GAMMA_ENL` |
|---|---|---|---|
| 0 (off, baseline) | 1.0 (default) | 1.0 | 1.0 |
| 1 (on) | 1.0 | 1.0 | 1.0 |
| 1 | 1.5 | 1.5 | 2.0 |
| 1 | 2.0 (max) | 2.0 (max) | 5.0 (max) |

Other params held at phase-1 values.

**Decision gate**: if any cell delivers ≥ −10 dB, drill in by
testing intermediate `GAMMA` values around the best cell.

### Phase 3 — Non-linear echo training (`PP_NLATTENONOFF` + `PP_NLAEC_MODE`) (~20 min)

Specifically targets speaker non-linearity at high SPL. The
`train` modes adapt to the actual speaker chain; `train2` is more
aggressive.

| `PP_NLATTENONOFF` | `PP_NLAEC_MODE` | Notes |
|---|---|---|
| 0 (off, baseline) | (any) | reference run |
| 1 (on) | 0 (normal) | most common operating mode |
| 1 (on) | 1 (train) | requires sustained audio for training |
| 1 (on) | 2 (train2) | aggressive training |

Each run: 60 sec for normal mode, 90 sec for train modes (need
extra time for non-linear model adaptation). Other params held at
the best-so-far state from phases 1-2.

**Decision gate**: if any train mode delivers ≥ −10 dB, that's the
configuration we keep.

### Phase 4 — AEC pre-emphasis (`AEC_AECEMPHASISONOFF`) (~10 min)

Three states: 0 (off), 1 (on, speech-band emphasis), 2 (on_eq,
emphasizes low-frequency reference content). The on_eq mode
specifically helps when the reference signal has different spectral
balance than what reaches the mic — possibly relevant to our
external-DAC topology.

| `AEC_AECEMPHASISONOFF` | Run |
|---|---|
| 0 (off) | baseline |
| 1 (on) | speech-band emphasis |
| 2 (on_eq) | low-freq boost on reference |

### Phase 5 — Disable Path Change Detection (`AEC_PCD_COUPLINGI`) (~10 min)

Tests the user's clock-drift hypothesis directly. PCD watches for
sudden changes in the echo path and resets the adaptive filter.
Per the docstring: *"PCD can be disabled by setting a value
outside the range [0.0, 1.0]"*.

| `AEC_PCD_COUPLINGI` | Effect |
|---|---|
| current default | baseline |
| -1.0 (disabled) | filter never resets due to PCD |

**Decision gate**: if disabling PCD increases peak attenuation OR
prevents the filter from going to zero coefficients during a
60-sec run, the user's drift hypothesis was at least partially
right and we keep PCD disabled.

### Phase 6 — Reference gain stage (`AUDIO_MGR_REF_GAIN`) (~10 min)

Untested in the prior investigation. Distinct from `AEC_FAR_EXTGAIN`
(which is the auto-mirror of UAC2 volume) — `AUDIO_MGR_REF_GAIN`
is described as *"pre-SHF reference gain"*, applied earlier in the
chain.

Sweep: −20, 0 (default), +6, +12, +20 dB. Other params at best-so-far.

### Phase 7 — Output channel switch (`AEC_ASROUTONOFF`) (~5 min)

The chip's USB capture currently outputs the conference channel.
With `AEC_ASROUTONOFF=0`, outputs are AEC residuals (one per mic)
instead. Different post-processing path; might give cleaner
measurements of pure AEC effect.

| `AEC_ASROUTONOFF` | What we read |
|---|---|
| 1 (default) | ASR-processed (post-BF + post-NS + post-AGC) |
| 0 | AEC residuals per mic (closer to pure AEC output) |

### Phase 8 — Quick one-shot tests (~5 min)

Single-value verifications.

- **`AUDIO_MGR_FAR_END_DSP_ENABLE=1`**: parameter exists but
  description includes "(if implemented)" suggesting possible no-op
  on UA firmware. One run; verify or rule out.
- **Verify `AUDIO_MGR_SYS_DELAY` clamp**: write 512, read back. If
  clamps to 256: confirmed. If accepts: re-run phase 1 at 384 and
  512 too. (Almost certainly clamps.)

### Phase 9 — Best-of-best combined run (~5 min)

If any of phases 2-7 delivered ≥ −5 dB, combine the winning
parameters from each into one configuration and run a final 90-sec
test to verify the combination is additive and not regressive.

---

## What we're explicitly NOT testing

| Parameter | Why not |
|---|---|
| `I2S_DAC_DSP_ENABLE=1` | Routes AEC ref from disconnected I²S input pins → silent reference → makes AEC worse, not better. Diagnostic value only; rules out a configuration nobody would use. |
| `AUDIO_MGR_SYS_DELAY` > +256 | Already verified empirically that the chip clamps. A re-verification is a single read-back command, not a sweep. |
| `SHF_BYPASS` toggling | Per XMOS docs, it's a timing-stress mode, not a clean AEC disable. We use `EXTGAIN=-120` for AEC-effectively-off instead. |
| `TEST_AEC_DISABLE_CONTROL` | XMOS docs warn: *"Only to be used for internal testing! When disabled, there's no way to bring back control without restarting the device."* Power-cycle hazard. |
| `SAVE_CONFIGURATION` | Brick hazard on certain firmware versions per respeaker repo issue #8. Never call this. |
| `USB_BIT_DEPTH` change | Reboots chip and resets ALL parameters to default. Test infrastructure cost outweighs marginal test value. |
| Custom firmware modifications | Out of scope; this is a config-only investigation. |

---

## Test harness

A new Python CLI: `jasper-aec-matrix` (or maybe just an inline
script run via `python -m`). Architecture:

```
1. Stop jasper-voice (frees XVF capture EP).
2. Read current values for every parameter we'll touch.
   Save as a "restore" dict for the finally block.
3. Read current camilla master_gain. Compute test volume as
   min(target_volume, current_volume). Refuse to raise.
4. For each phase in test plan:
     For each cell in phase's parameter grid:
       Apply parameters via xvf_host.write
       Wait 1 sec for chip to stabilize
       (For Phase 3 train modes: also enable the mode 30 sec before
        the measurement starts so training can begin)
       Open arecord on hw:CARD=Array,DEV=0 ch 0 (post-AEC conference)
       Play 60-sec sustained log sweep via _audioout
       Compute RMS
       Dump SPECIAL_CMD_AEC_FILTER_COEFFS pre and post
       Append result row to results table
5. In finally:
   Restore all touched parameters from the restore dict.
   Restore camilla master_gain.
   Restart jasper-voice.
6. Print summary table:
   Phase | Cell | AEC-OFF RMS | AEC-ON RMS | Attenuation |
   Filter peak mag | Filter RMS | Converged?
7. Save full results JSON to /var/lib/jasper/aec-matrix-{date}.json.
```

Total runtime: ~2 hours of bench time. Most of it is the long-soak
sweep playback. Parameter writes are sub-millisecond.

---

## Safety and reversibility

- All parameter changes are reversible by either:
  - explicit restore from the saved values (the `finally` block), or
  - `xvf_host REBOOT 1` followed by `jasper-aec-init` (which
    re-applies our standard config: PCM volume to unity, etc.)
- Volume is read first and clamped — never raised above current.
- 60-sec sweep at 5% FS into a master_gain ducked to `min(-32 dB,
  current)` lands ~−50 dBFS at the speaker — well below room tone
  for a normal listening environment.
- Open windows / outside noise will inflate both AEC-OFF and AEC-ON
  RMS measurements equally; the dB difference is preserved. Still
  better to run with windows closed for cleaner numbers.
- No `SAVE_CONFIGURATION`. No `TEST_AEC_DISABLE_CONTROL`. No
  firmware re-flash. Failure mode is "test gives unclear results"
  — never "device is bricked."
- Brief jasper-voice outage during testing (~2 hours). Music
  playback through camilla is unaffected (camilla doesn't hold the
  XVF capture EP).

---

## Decision tree at end of test

```
Best attenuation in any cell:
├── ≥ −15 dB
│   └── Adopt that combination as default. Apply via jasper-aec-init.
│       Tear down software AEC: delete jasper-aec-bridge service,
│       drop snd-aloop two-card config, remove SpeexDSP install.
│       Update CLAUDE.md/README.md to reflect chip-side-by-default.
│       (See HANDOFF-aec.md § "Three revert options" — option B
│       maps cleanly onto this.)
│
├── −5 to −15 dB
│   └── Compare against software AEC's peak measured −8 dB.
│       Decision factors: RAM cost (chip-side: ~0; software: 110 MB),
│       reliability (chip-side: holds; software: re-adapts during gaps),
│       complexity (chip-side: a config file; software: a daemon).
│       Likely answer: chip-side wins on every axis if it gets
│       there. Adopt as default.
│
└── < −5 dB
    └── Chip-side is conclusively done in this topology. Document
        in HANDOFF-aec.md. Decision is now between software AEC
        (current) and accepting no-AEC default. Likely answer:
        keep software AEC opt-in for users on 2GB+ Pi 5 who want
        to A/B; default stays no-AEC for 1GB Pi.
```

---

## Honest expected outcome

Most likely: **best attenuation is in the −3 to −7 dB range**, in
phase 2 or phase 3. The post-AEC suppressor (`PP_ECHOONOFF` family)
is the dominant unknown — it operates independently of adaptive
filter convergence and is specifically designed to compensate for
exactly the imperfect-cancellation regime our topology produces.

If that's the result, we land in the middle decision branch
(−5 to −15 dB), and the decision becomes a comparison against
software AEC. Chip-side likely wins because it has zero RAM cost
and inherent stability — but we've never empirically compared them
under matched conditions, so this matters.

A pleasant surprise (≥ −15 dB) is possible but unlikely. The XMOS
docs are clear on the architectural mismatch; parameter sweeps
don't fix architectural mismatches. If we get there, it'll be
because the post-suppressor compensates for the underlying AEC's
poor convergence.

A confirming dud (<−5 dB) is the second-most-likely outcome.
That's still valuable — it lets us close the chip-side door
definitively and stop wondering if we missed something.

---

## Cost of NOT doing this

The chip-side investigation has been the longest and most
contentious part of the AEC work. Without these final tests, every
future revisit ("did we really try X?") will require re-running
the same machinery with the same knowledge gaps. Two hours of bench
time now closes that loop forever.

If we skip these and commit fully to software AEC, the failure
mode is: a future session re-discovers `PP_ECHOONOFF` exists,
spends a day testing it without proper methodology, gets noisy
results, and adds another parallel half-finished AEC code path.
That's worse than the 2-hour cost.

---

## Pre-flight checklist before running

- [ ] Pi 5 in normal operating state, jasper-voice running, music
      can be played
- [ ] Windows closed, ambient noise minimized
- [ ] Master volume in your normal listening range so the test
      volume (-32 dB or current, whichever is lower) is genuinely
      quieter
- [ ] Confirm chip is on firmware 2.0.8 6-channel
      (`xvf_host VERSION` returns `[2, 0, 8]`)
- [ ] Confirm `xvf_host` works: `sudo /opt/jasper/.venv/bin/python
      -m jasper.xvf.xvf_host VERSION`
- [ ] No critical work blocked by ~2 hour jasper-voice outage
