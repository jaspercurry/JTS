# AEC chip-side test — execution briefing for fresh sessions

This document is a self-contained briefing for an LLM session
that's been asked to run the chip-side AEC parameter test on a
JTS smart-speaker Pi. Read it through and you'll have everything
you need.

The test harness, parameter list, decision criteria, and safety
rails are all already implemented. Your job is to execute the
test, interpret the results, and act on the decision tree.

---

## Project context (read first)

JTS is a personal-hobby smart speaker built on a Raspberry Pi 5
running Raspberry Pi OS Lite Trixie plus
[CamillaDSP](https://github.com/HEnquist/camilladsp), with a voice
loop powered by Gemini 3.1 Flash Live. The hardware:

| Component | Role |
|---|---|
| Raspberry Pi 5 (1GB) | Host running everything |
| Apple USB-C → 3.5mm dongle | DAC for the speaker (48 kHz) |
| TPA3255 amp + speakers | Speaker chain |
| Seeed ReSpeaker XVF3800 (USB UA, firmware 2.0.8 6-channel) | 4-mic array w/ XMOS DSP |

The XVF3800's onboard codec is **disconnected** — speakers come
from the Apple dongle. This non-standard topology is what drives
the AEC story.

For full project context: [README.md](../README.md). For the AEC
investigation history specifically: [HANDOFF-aec.md](HANDOFF-aec.md).
**You don't need to read those right now to execute this test** —
this document has the relevant subset.

---

## What problem this test solves

A smart speaker that plays music and listens for a wake word in
the same physical box has a fundamental problem: the mic hears
20-40 dB more music than user voice. Without acoustic echo
cancellation (AEC), wake-word detection fails during music and
the system feedback-loops on its own TTS.

There are three places to do AEC:

1. **Hardware AEC on the mic chip** — the XVF3800 has a
   purpose-built DSP for this. Lowest cost, lowest latency,
   highest quality when working.
2. **Software AEC on the host** — flexible but costs CPU/RAM.
3. **Avoid AEC** — push-to-talk, mic-speaker physical isolation,
   or aggressive ducking.

This project has tried (1) and (2). Both work imperfectly.
**This test is the final word on (1)** — does any chip-side
parameter combination deliver useful attenuation in our topology?

---

## What's been tried already

The chip-side AEC investigation (full record:
[HANDOFF-aec.md](HANDOFF-aec.md)) found:

1. **`AUDIO_MGR_SYS_DELAY` swept −64 to +256 samples** with
   `AEC_FAR_EXTGAIN` at 0 dB after fixing the UAC2 PCM volume
   mirror: best ≤ −2 dB sustained attenuation. Values >256
   silently clamp.
2. **The `AEC_FAR_EXTGAIN` auto-mirror discovery**: per XMOS
   User Guide §4.2.1, the chip's UA-mode AEC reference gain
   tracks the host's UAC2 OUT volume control. Default Linux
   ALSA leaves UAC2 PCM at −20 dB → chip parks `EXTGAIN` at −40
   dB → AEC reference becomes inaudible to its own pipeline.
   Setting UAC2 PCM to unity (60/60) makes EXTGAIN read 0 dB.
3. **The architectural smoking gun**: per XMOS docs, the chip's
   AEC pipeline assumes **the chip's own DAC drives the speaker**.
   In an external-DAC topology (ours), the AEC has internal
   assumptions about the speaker output that don't match what
   actually plays. Public XMOS docs do not describe a working
   configuration for our topology.
4. **Software AEC bridge built and tested**: SpeexDSP-based,
   captures host loopback as far-end + chip raw mic 0 as
   near-end, runs adaptive cancellation, emits AEC'd mono. Best
   measured: −2 to −8 dB attenuation. Built but **disabled by
   default** because the 1GB Pi 5 is at the edge with the
   bridge's 110 MB RAM cost.

What the prior investigation **didn't** test — and what this
test covers:

- The post-AEC echo suppressor family (`PP_ECHOONOFF` +
  `PP_GAMMA_*`) — operates independently of adaptive filter
  convergence
- Non-linear echo training modes (`PP_NLATTENONOFF` + `PP_NLAEC_MODE`)
- AEC pre-emphasis (`AEC_AECEMPHASISONOFF`)
- Path Change Detection disable (`AEC_PCD_COUPLINGI` out of range)
- Pre-SHF reference gain (`AUDIO_MGR_REF_GAIN`)
- Output channel switch (`AEC_ASROUTONOFF=0`)
- Long-soak convergence (60+ sec at fixed parameters with filter
  dump pre/post — prior tests reset adaptation every 5 sec)
- Methodology: **AEC-OFF via `EXTGAIN=−120` instead of `SHF_BYPASS`**
  (which is a timing-stress mode that was never measuring AEC
  contribution — it was measuring "raw mic vs full pipeline incl.
  4-mic beamformer")

---

## Decision criteria

**The single decision this test produces:**

| Best attenuation observed | Decision |
|---|---|
| **≥ −15 dB** | Adopt that parameter combination as the default. Tear down software AEC bridge entirely. |
| **−5 to −15 dB** | Compare against software AEC's measured peak −8 dB. Most likely outcome: chip-side wins on RAM (~0 vs +110 MB) and stability (holds vs re-adapts during gaps). Adopt. |
| **< −5 dB** | Chip-side AEC is conclusively dead in this topology. Software AEC stays opt-in for 2GB+ Pi users; 1GB Pi default is no-AEC. |

Honest expected outcome: **most likely −3 to −7 dB** (the
post-suppressor family is the dominant unknown), landing in the
middle bucket. A pleasant surprise (≥ −15 dB) is possible but
unlikely — the architectural mismatch isn't something a parameter
sweep can fix. A confirming dud (< −5 dB) is also plausible and
still valuable: it definitively closes the chip-side door.

---

## How to execute

The test harness is `jasper-aec-matrix` (source:
`jasper/cli/aec_matrix.py`), installed as a console script in the
Pi's venv at `/opt/jasper/.venv/bin/jasper-aec-matrix`.

### Pre-flight

Run on the Pi:

```sh
# 1. Confirm chip is on firmware 2.0.8 6-channel
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host VERSION
# expect: VERSION: [2, 0, 8]

# 2. Confirm AGC + bridge state (bridge should be disabled by default)
systemctl is-active jasper-aec-bridge
# expect: inactive

# 3. Run jasper-doctor to confirm system is healthy
sudo -E /opt/jasper/.venv/bin/jasper-doctor
# expect: 1 fail (mic-capture-busy, pre-existing) + warnings, but
# all AEC-related checks should be ✓

# 4. Confirm chip's UAC2 PCM is at unity (jasper-aec-init does this
# at boot, but verify):
amixer -c Array sget PCM,0 | grep dB | head -1
# expect: 0.00 dB. If not, run:
amixer -c Array sset PCM,0 60 unmute
amixer -c Array sset PCM,1 60 unmute
```

### Environment for the test

- **Windows closed.** Outside noise floor inflates the AEC-OFF
  baseline RMS, but it inflates AEC-ON RMS equally. The dB
  difference is preserved either way. Still: closed windows give
  cleaner numbers.
- **No active music playback** — the harness will play its own
  test sweeps via `_audioout`. Other audio sources will collide.
- **Speaker volume**: the harness reads current `master_gain`,
  caps the test volume to `min(-32 dB, current)`, never raises.
  If you're at −40 dB resting (for example), test runs at −40 dB.
  At −32 dB master_gain × 5% FS sweep = ~−50 dBFS at the speaker
  → below typical music level. Headphones not required, but if
  the user wants extra protection they can plug in headphones
  (the dongle output is unaffected by what happens at the chip).

### Run the test

```sh
sudo /opt/jasper/.venv/bin/jasper-aec-matrix
```

Total runtime: ~2 hours. The harness will:

1. Stop `jasper-voice` (frees the XVF capture endpoint)
2. Read all parameters that will be modified, save originals
3. Duck `master_gain` per the cap
4. For each cell in the test plan:
   - Apply parameters
   - Run AEC-OFF measurement (`EXTGAIN=−120`, 60-sec sweep)
   - Run AEC-ON measurement (`EXTGAIN=0` or cell-specific)
   - Dump AEC adaptive filter coefficients pre and post
   - Compute attenuation in dB
5. Restore all parameters from snapshot
6. Restore `master_gain`
7. Restart `jasper-voice`
8. Write results to `/var/lib/jasper/aec-matrix-<timestamp>.{json,md}`
9. Print the summary table to stdout

### Quick variants

```sh
# Run only one phase (1-8). Useful for iterating.
sudo /opt/jasper/.venv/bin/jasper-aec-matrix --phase 2

# Dry-run: walk the plan, print params, take no action.
sudo /opt/jasper/.venv/bin/jasper-aec-matrix --dry-run

# Faster cells (10-second captures instead of 60). Use for sanity-
# checking the harness; results aren't trustworthy for the
# convergence-sensitive phases (1, 3) at short captures.
sudo /opt/jasper/.venv/bin/jasper-aec-matrix --capture-seconds 10
```

---

## Test plan summary

The harness implements 8 phases (phase 9 is reserved for a
manual best-of-best combined run after analyzing phase 1-8
results):

| Phase | What it tests | Cells | Why |
|---|---|---|---|
| 1 | Long-soak baseline at default params + tuned `SYS_DELAY` | 1 | Establishes whether AEC ever converges given adequate time |
| 2 | `PP_ECHOONOFF` + `PP_GAMMA_E/ETAIL/ENL` | 4 | Post-AEC suppressor; **highest-priority untested area** |
| 3 | `PP_NLATTENONOFF` + `PP_NLAEC_MODE` (incl. train modes) | 4 | Non-linear echo training for speaker non-linearity |
| 4 | `AEC_AECEMPHASISONOFF` (off, on, on_eq) | 3 | Pre-emphasis filter for spectral matching |
| 5 | `AEC_PCD_COUPLINGI` out-of-range to disable PCD | 1 | Tests the "drift causes filter resets" hypothesis directly |
| 6 | `AUDIO_MGR_REF_GAIN` swept −20 to +20 dB | 5 | Pre-SHF reference gain (separate from `EXTGAIN`) |
| 7 | `AEC_ASROUTONOFF` 0/1 | 2 | Toggles output between AEC residuals + ASR-processed |
| 8 | `AUDIO_MGR_FAR_END_DSP_ENABLE=1` quick check | 1 | Param has "(if implemented)" caveat — verify or rule out |

Each cell measures both AEC-OFF (EXTGAIN=−120) and AEC-ON
(EXTGAIN=0 or cell-specific). The dB difference is the
**linear AEC's contribution under that parameter combination**.

Phases 1, 3 (train modes) need the full 60 sec for adaptation;
others could run faster but the harness uses 60 sec uniformly
for cleaner comparison.

---

## Reading the output

The MD file (printed to stdout at end + saved to
`/var/lib/jasper/aec-matrix-<timestamp>.md`) has columns:

| Phase | Label | OFF rms | ON rms | Atten (dB) | Filter peak | Filter RMS | Conv? | Chip flag |

- **Atten (dB)**: 20·log10(ON/OFF). **Negative = attenuation
  (good), positive = AEC made it louder (bad).**
- **Filter peak**: peak magnitude of any tap in the AEC adaptive
  filter dump. Healthy convergence: **< 1.0**. Runaway LMS: > 1.0.
- **Filter RMS**: RMS of all 3072 filter taps. Stuck at zero =
  filter never adapted (or kept getting reset). Substantial
  (>0.05) = converged.
- **Conv?** (heuristic): peak <1.0 + RMS >1e-4 + at least 10% of
  taps non-zero. Approximation of "filter looks healthy."
- **Chip flag**: `AEC_AECCONVERGED` per the chip's own
  determination. Has historically returned 0 in our setup; will
  be interesting to see if any parameter flips it.

The MD file ends with a summary line: best attenuation across all
cells, plus the corresponding decision per the criteria above.

---

## What to do with the results

### If best ≥ −15 dB (unlikely)

1. **Adopt the winning parameter combination as default.** Edit
   `jasper/cli/aec_init.py` to apply the parameters at boot
   alongside the existing volume-mirror reset.
2. **Tear down software AEC.** See [HANDOFF-aec.md](HANDOFF-aec.md)
   § "Three revert options" — option 2 (code revert, firmware
   kept) maps cleanly here.
3. Update README.md and CLAUDE.md to reflect chip-side as default.
4. Update `jasper/cli/doctor.py` checks (drop bridge, add
   "AEC_AECCONVERGED reads 1" check).

### If best −5 to −15 dB (most likely)

1. **Compare against software AEC's measured −8 dB peak.**
   Decision factors:
   - **RAM**: chip-side ~0, software +110 MB
   - **Stability**: chip-side holds, software re-adapts during
     silent gaps
   - **Convergence speed**: both roughly comparable (20-30 sec)
2. **Most likely the chip-side wins** on every axis if it gets
   into this range. Adopt as default per the "≥ −15 dB" steps
   above.
3. **If software AEC outperforms** (e.g. chip is at −5 dB and
   software was actually closer to −8 dB sustained), keep
   software AEC opt-in for 2GB Pi users. Update HANDOFF-aec.md.

### If best < −5 dB

1. **Document conclusively in HANDOFF-aec.md.** Add a "Final
   chip-side test results" section with the table.
2. **No code changes needed** — the current default
   (`JASPER_MIC_DEVICE=Array`, bridge disabled) stays.
3. The decision then becomes: keep software AEC opt-in (current
   state) vs full removal. **Recommend keeping it opt-in** so
   2GB Pi users can test it without rebuilding the architecture.

---

## Safety rails (built into the harness)

The harness has these safety properties; do not disable them:

- **Volume cap**: reads current `master_gain`, never raises.
  Test volume = min(−32 dB, current).
- **Volume restore**: in `finally`, restores original
  `master_gain`. Survives keyboard interrupt and exceptions.
- **Parameter snapshot + restore**: every parameter the harness
  writes is read first; original values are stored and restored
  in `finally`.
- **Service lifecycle**: stops `jasper-voice` only if it was
  active; restarts it on exit. CamillaDSP isn't touched (doesn't
  hold the XVF capture endpoint).
- **Never calls `SAVE_CONFIGURATION`**: brick hazard on certain
  firmware versions per respeaker repo issue #8.
- **Never calls `TEST_AEC_DISABLE_CONTROL`**: per XMOS docs,
  there's no recovery from this without a power cycle.
- **Never modifies firmware**: this is a config-only test.

If the harness exits abnormally (system crash, kill -9 on the
process), the chip parameters may be left in a non-default
state. Recovery: `sudo systemctl start jasper-aec-init` reboots
the chip via `xvf_host REBOOT 1` and re-applies the standard
config (UAC2 PCM volume to unity).

---

## Caveats and known limitations

1. **The AEC residual measurement assumes channel 0 = "conference"**
   — the chip's processed output. On the 6-channel firmware, this
   includes beamforming and noise suppression on top of any AEC
   work. We can't measure pure AEC contribution this way (would
   need raw mic from another channel + careful processing). What
   we ARE measuring: total system attenuation with vs without a
   working AEC reference signal. That's the user-facing metric.

2. **`AUDIO_MGR_FAR_END_DSP_ENABLE=1`** has the docstring caveat
   "(if implemented)" — possibly a no-op on UA firmware. The
   one-shot test in phase 8 will tell us either way.

3. **Phase 3 train modes (NLAEC_MODE 1/2)** need extra time for
   the non-linear model to begin adapting before measurement
   starts. The harness adds a 30-sec warmup for these specific
   cells. If the test feels longer than expected during phase 3,
   that's why.

4. **Filter coefficient dump is for AEC adaptive filter, not
   post-AEC suppressor.** Phase 2 (post-suppressor) cells may
   show the filter unchanged but real attenuation in the OFF/ON
   diff — that's correct behavior. The post-suppressor doesn't
   live in the adaptive filter.

5. **Open windows** inflate room ambient and reduce measurable
   attenuation magnitude. Same effect on AEC-OFF and AEC-ON, so
   the dB diff is preserved, but signal/noise of the test is
   worse. Cleaner numbers with windows closed.

6. **First time the chip's been swept this carefully** — if the
   harness uncovers a parameter combination that does something
   weird (chip locks up, USB drops, etc), recovery is via
   `jasper-aec-init` restart or worst-case unplug-the-chip.

---

## File map

The relevant files in this repo:

```
jasper/cli/aec_matrix.py        ← THE TEST HARNESS (this is what you run)
jasper/cli/aec_init.py          Boot-time chip init (reset + UAC2 unity volume)
jasper/cli/aec_tune.py          Original AUDIO_MGR_SYS_DELAY calibrator
jasper/cli/aec_bridge.py        Software AEC bridge (currently disabled)
jasper/xvf/xvf_host.py          XMOS chip control library (vendored)

deploy/systemd/jasper-aec-init.service   Boot-time init unit
deploy/systemd/jasper-aec-bridge.service Software AEC unit (disabled)

docs/HANDOFF-aec.md                       Full investigation history
docs/aec-chipside-final-test-plan.md      Test plan (preceded this doc)
docs/aec-chipside-test-briefing.md        ← THIS FILE

CLAUDE.md / AGENTS.md           AI agent operational notes
README.md                       Project overview
```

---

## When you're done

After the test completes, do these in order:

1. **Review the summary table** in
   `/var/lib/jasper/aec-matrix-<timestamp>.md` and the JSON
   for full data.
2. **Decide per the decision tree above**.
3. **Update [HANDOFF-aec.md](HANDOFF-aec.md)** with a "Final
   chip-side test results" section that:
   - Cites the JSON file path
   - Quotes the best attenuation cell
   - States the decision taken
4. **If the decision changes the default behavior** (chip-side
   becomes the default, or software AEC removed), update:
   - `jasper/cli/aec_init.py` (apply the winning params at boot)
   - `deploy/systemd/jasper-aec-bridge.service` (if removed)
   - `deploy/install.sh` (uninstall steps if removed)
   - `pyproject.toml` (drop dependencies if no longer used)
   - `.env.example` (if `JASPER_MIC_DEVICE` should change)
   - `README.md` § "Acoustic echo cancellation"
   - `CLAUDE.md` / `AGENTS.md` § "AEC bridge — opt-in toggle"
5. **Commit and push.** Conventional commit format the project
   uses: short imperative subject + body explaining what + why.
