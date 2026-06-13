# Research & Assessment: Multi-Speaker Balance + Sync Calibration

> **Status: research artifact / session pickup.** Snapshot from
> 2026-06-13 evaluating a proposal for stereo-pair *balance* (level)
> and *sync* (arrival-time) calibration in JTS. Combines a codebase
> audit against the proposal's claims with web prior-art research
> (REW methodology, excitation-signal theory, GCC-PHAT, phone-mic
> error budget, the Snapcast API, and commercial prior art). Read
> this for the reasoning and the citations, not as current
> operational truth — code facts below were verified on branch
> `claude/balance-sync-calibration-mbp3ag` and will drift. Current
> operational truth for multi-room lives in
> [docs/HANDOFF-multiroom.md](HANDOFF-multiroom.md) and
> [docs/dumb-endpoint-bringup.md](dumb-endpoint-bringup.md).

---

## TL;DR verdict

The proposal is technically sound and worth building, but it's calibrated
to a slightly stale picture of the repo. Two framing claims need
correcting; the rest is well-supported by prior art and already matches
JTS's documented design intent.

- **Solid instincts (all validated):** keep *balance* ("which is louder?")
  separate from *sync* ("which arrives first?"); use a chirp/sweep +
  cross-correlation, never a steady tone; split the correction into two
  knobs (Snapcast per-client latency for the fixed endpoint/DAC-path
  offset, leader-side per-channel delay for the acoustic/seat offset).
- **Correction 1 — endpoint trim:** on a *full* speaker, bonded trim
  already works end-to-end through `outputd`. On a **Zero 2 W endpoint
  (JTS4)**, the canonical design applies per-endpoint trim via **Snapcast
  `Client.SetVolume`**, *not* `outputd` (the Zero never runs `outputd`),
  and the endpoint install tier itself **isn't built yet**. So "trust
  balance on JTS4" is a tier-build prerequisite, not a small patch.
- **Correction 2 — sync isn't greenfield:** it's the already-scoped
  **Increment 4 — acoustic-sync confirmation**, and a throwaway reference
  implementation already exists at `scripts/multiroom-spike-measure.py
  acoustic`. The work is to *productionize* it, not design from scratch.
- **Feasibility (quantified):** relative inter-speaker arrival-time
  accuracy of **~10–50 µs** is realistic with a phone mic in a browser —
  roughly **20× better than stereo imaging needs** (the perceptual target
  is a small fraction of 1 ms). Differentiated: **no commercial smart
  speaker publicly does mic-at-the-seat inter-speaker arrival-time
  alignment.**

---

## 1. The proposal under evaluation

Summarized from the original note:

1. **Balance gap:** Zero 2 W direct endpoints may not apply `trim_db`. Full
   speakers apply trim via `outputd`
   (`JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB`); the endpoint path is
   `snapclient → ALSA`, so trim is written but maybe not heard on JTS4.
   Best next work: (1) add endpoint trim application, (2) doctor coverage
   "endpoint trim configured and active", (3) live-test `/balance/` on
   JTS + JTS4.
2. **Prior art:** REW is the closest model — separates level from timing,
   uses a timing reference for a common time axis, derives delay from
   impulse-response peak (subs less precise, broad peak). Don't use a
   steady tone (phase-ambiguous every cycle); use click/chirp/sweep/MLS
   detected by cross-correlation.
3. **Sync feature shape:** a leader-only `/sync/` flow — phone mic opens
   (same privacy posture as `/balance/`), leader plays a safe test
   sequence with scheduled L/R markers (short broadband or 500 Hz–4 kHz
   chirps), browser/server cross-correlates against the known marker,
   computes `relative arrival error = detected_(R−L) − scheduled_(R−L)`,
   repeats 3× with confidence gating, reports "Right arrives 1.4 ms late
   at this position", and later offers "Apply compensation" using
   Snapcast per-client latency for endpoint-path offset and leader-side
   per-channel delay for acoustic alignment. Keep balance and sync
   cleanly separate.

---

## 2. Codebase audit — what exists today

Verified on `claude/balance-sync-calibration-mbp3ag`.

### Balance (`/balance/`) — fully implemented
- Frontend `deploy/assets/balance/js/main.js`; backend
  `jasper/web/balance_flow.py`; math `jasper/multiroom/balance.py`.
- Phone-mic **equal-loudness ramp**: band-limited noise (500–2000 Hz,
  −42 to −12 dBFS over ~20 s). Phone listens with **EC/NS/AGC OFF**,
  meters in-band RMS via an AudioWorklet biquad band-pass.
- **Hearing-safe** bounded noise (−12 dBFS ceiling, same as the
  correction sweep). **Measurement window** held for the whole session.
  **Peer-first apply** (`balance_flow.py` sorts by `is_self`).
- Writes `/var/lib/jasper/grouping.env` (`JASPER_GROUPING_TRIM_DB` per
  member) → derived to `/var/lib/jasper/grouping-outputd.env`
  (`JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB`).

### Rooms (`/rooms/`) — fully implemented
- `jasper/web/rooms_setup.py` (`_set_member_trim`); frontend
  `deploy/assets/rooms/js/main.js`.
- Manual **attenuate-only** trim, ±0.5 dB nudges, clamped
  `TRIM_DB_MIN=-24 … TRIM_DB_MAX=0` (`jasper/multiroom/config.py`).
  Writes the same `grouping.env` via `/grouping/set`.

### Trim flow (full speaker) — confirmed working
`grouping.env` → `GroupingConfig.trim_db` → `reconcile.py`
`outputd_grouping_env()` writes `JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB` to
`grouping-outputd.env` → `rust/jasper-outputd/src/config.rs` applies it in
the `dac_content` lane before the DAC. Always written while bonded so a
cleared trim converges back to 0.0.

### Multiroom architecture (Increment 5, shipped 2026-06-11)
- Pure-data + reconciler pattern. `jasper/multiroom/{config,reconcile,
  state,leader_config}.py`. Roles `leader` / `follower`. One-flow `/bond`
  fans config to each member's `/grouping/set`.
- **Canonical signal flow:** leader's CamillaDSP bakes one shared stereo
  stream → snapserver pipe → each member's snapclient writes the
  round-trip `MEMBER_CONTENT_FIFO` (`--player file:`) → `outputd`
  `dac_content` lane channel-picks and plays. Dumb channel-drop at
  endpoints; **no voice on the shared stream** (member-local TTS via each
  member's own `outputd` socket, `grouping-voice.env`).

### Snapcast control — minimal today
- `jasper/multiroom/snapcast_rpc.py` calls only `Server.GetStatus` and
  `Group.SetStream` (a 2026-06-11 binding-pin fix). Per-client latency is
  set via the snapclient **`--latency` CLI arg**, *not* `Client.SetLatency`
  RPC. `Client.SetVolume` is **not** wired yet.

### Sync / delay alignment — minimal, mostly planned
- No production cross-correlation/chirp measurement. The leader's
  CamillaDSP **delay filter** is the intended acoustic-alignment knob
  (manual today). Reference implementation exists:
  `scripts/multiroom-spike-measure.py acoustic` (THROWAWAY bench rig) —
  single-mic broadband-**click** capture → cross-correlation
  (inter-arrival jitter) + autocorrelation (L/R offset). Its docstring
  already states the proposal's key rule: *"A broadband click — not a
  tone — is required: a tone's autocorrelation is ambiguous at its own
  period."*

### Doctor — limited
- `jasper/cli/doctor/grouping.py`: `check_grouping` (config + unit state)
  and `check_grouping_rate_adjust`. **No** trim/balance/endpoint-specific
  probe.

---

## 3. The two corrections in detail

### Correction 1 — endpoint trim is `Client.SetVolume`, gated on an unbuilt tier
Per `docs/dumb-endpoint-bringup.md` (verified 2026-06-12):

- The Zero 2 W **endpoint tier never installs `jasper-outputd`** ("no Rust
  on the Zero"). Its path is `snapclient → ALSA` directly.
- Per-endpoint level trim is meant to ride **Snapcast `Client.SetVolume`**
  (a leader-side, invertible-taper knob persisted in the server's state) —
  *not* `JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB`.
- The **endpoint install tier (Phase 2) is PLANNED, not built.** Today a
  Zero can't actually bond. The reconciler writes the outputd trim env for
  every member regardless, but on a Zero nothing reads it.

**Therefore:** on full speakers (Pi 5/Pi 3), trim already works — the
proposal's worry doesn't apply. On JTS4, the fix is "wire `Client.SetVolume`
through the existing `snapcast_rpc.py` RPC plumbing into the balance apply
path", and the real prerequisite for "trust balance on JTS4" is the
endpoint install tier landing. Treat JTS4 as a lab endpoint until then.

### Correction 2 — sync is Increment 4, with a reference implementation
`docs/HANDOFF-multiroom.md` scopes **Increment 4 — acoustic-sync
confirmation** (~20 min owner session): "chirp at moderate volume, one mic
between the pair, cross-correlate (`multiroom-spike-measure.py acoustic`)",
working target **p99 < 1 ms L/R**. The repo also already commits to the
proposal's two-knob split, almost verbatim: *"snapclient `--latency` nulls
fixed DAC-path differences; the leader's CamillaDSP delay filter aligns
acoustic arrival at the seat … Set the CamillaDSP delay from the measured
impulse arrivals; keep `--latency` for hardware-path differences."*

So: productionize Increment 4 into a leader-only wizard (as the proposal
envisions), upgrading the throwaway click/plain-xcorr script per the prior
art below. Not greenfield.

---

## 4. Prior-art synthesis

### 4.1 REW methodology (the closest model)
- **Separates level from timing** as distinct controls on the All-SPL
  graph: *Align SPL…* (level) vs *Time align* / *Cross corr align*
  (timing). Validates keeping balance and sync separate.
- **Common time axis** via an **acoustic timing reference** — a **5–20 kHz
  sweep (~700 ms)** played before each measurement; "a subwoofer cannot be
  used as the reference" because HF content is what makes timing precise.
  Reports per-measurement **System Delay (ms)** + equivalent distance.
- **Delay from the IR peak** for full-range speakers; for **band-limited
  devices (subs)** it switches to the **IR start**, because a low-pass
  impulse's energy peak arrives well after onset (using the peak
  over-states distance). REW's author: delay measurements are "unreliable
  for low-bandwidth devices like subwoofers." Time precision is
  **bandwidth-limited** — the central design lesson.
- **Clock-drift correction:** "Adjust clock with acoustic ref" plays a
  *second* reference at the **end** and rescales time from the
  inter-reference interval. Borrowable fallback (bracket the L/R markers
  with a known reference at start + end).
- REW itself uses **cross-correlation** ("Align IRs at cursor",
  "Cross corr align") and a minimum-phase comparison for "Estimate IR
  delay".

Sources: [Making Measurements](https://www.roomeqwizard.com/help/help_en-GB/html/makingmeasurements.html) ·
[All SPL / Alignment](https://www.roomeqwizard.com/help/help_en-GB/html/graph_allspl.html) ·
[Impulse Graph](https://www.roomeqwizard.com/help/help_en-GB/html/graph_impulse.html) ·
[Analysis prefs](https://www.roomeqwizard.com/help/help_en-GB/html/analysis.html) ·
[Soundcard prefs](https://www.roomeqwizard.com/help/help_en-GB/html/soundcard.html) ·
[AVNirvana — sub delay (Mulcahy)](https://www.avnirvana.com/threads/rew-subwoofer-delay-measurements-dont-make-sense.14047/)

### 4.2 Excitation signal — use a log sine sweep (ESS)
| Signal | Crest factor | TOA precision | Distortion handling | Notes |
|---|---|---|---|---|
| **Log sweep (ESS, Farina)** | ~3 dB | Excellent | **Folds harmonic distortion to negative time → windowable** | Best overall |
| Linear chirp | low | Good | Weaker isolation | Simpler "chirp marker" |
| MLS | ~0 dB theo. | Good | Distortion smears across whole IR | Sensitive to time-variance/drift |
| Golay pair | low | Good | Side-lobes cancel on sum | Needs 2 consistent probes — fragile handheld |
| Click/impulse | huge | Sharp but low energy | None | Poor SNR in a reverberant room |
| **Steady tone** | — | **Unusable** | — | Phase repeats every cycle → ambiguous |

- **Crest factor is the JTS-relevant axis.** Under a *fixed peak ceiling*
  (JTS caps at −12 dBFS for hearing safety), a ~3 dB-crest sweep delivers
  far more measurement energy than a click — i.e. **more timing precision
  per unit of allowed loudness.** This is the strongest argument to replace
  the throwaway script's click.
- **ESS uniquely windows out distortion** (lands at negative time before
  the linear IR) — important because a phone speaker / driver at
  measurement level distorts; MLS would smear that distortion across the
  IR and could shift the direct-path peak.
- A **steady tone is disqualified** by 2π phase ambiguity (a 1 kHz tone
  wraps every 1 ms; 0.3 ms and 1.3 ms are indistinguishable).

Sources: [Farina AES #5093](https://www.aes.org/e-lib/browse.cfm?elib=10211) ·
[Farina "Advancements…"](https://www.academia.edu/54840062/Advancements_in_Impulse_Response_Measurements_by_Sine_Sweeps) ·
[Müller & Massarani](https://www.melaudia.net/zdoc/comparisonMesure.PDF) ·
[Think SRS swept-sine app note](https://thinksrs.com/downloads/pdfs/applicationnotes/SR1_SweptSine.pdf) ·
[ESS vs MLS (architectural acoustics)](https://www.researchgate.net/publication/280555212) ·
[van Veen — crest factor](https://www.merlijnvanveen.nl/en/study-hall/191-crest-factor-part-1) ·
[Phase ambiguity (Nortek)](https://support.nortekgroup.com/hc/en-us/articles/5841281561244)

### 4.3 GCC-PHAT — use regularized, SNR-weighted, with sub-sample interpolation
- Delay estimator: `τ̂ = argmax_τ R(τ)`. **GCC-PHAT** whitens the
  cross-spectrum (`ψ = 1/|G(f)|`, keep only phase), sharpening the
  direct-path peak and suppressing room coloration — the field-standard
  reverberation-robust TDOA method (Knapp & Carter 1976).
- **Low-SNR weakness (matters for JTS):** the `1/|G(f)|` whitening boosts
  noise-dominated bins when signal energy is low — and JTS deliberately
  runs *quiet* (safety ceiling). So use the **regularized** form
  `1/(|G(f)| + ε)` and/or **SNR-/coherence-weighted bins**, not textbook
  PHAT. The ESS choice helps here: its processing gain raises peak SNR,
  keeping regularized PHAT in its good regime under the loudness cap.
- **Sub-sample refinement:** raw IFFT quantizes to 1 sample (≈20.8 µs at
  48 kHz). Prefer **zero-pad upsampling (≈8×)** (optionally a Gaussian fit)
  over a bare **parabolic** fit — parabolic is cheap but *biased*.
- Accuracy is bounded by the **Cramér-Rao bound** (∝ SNR, bandwidth,
  observation time) → another reason to go wideband (ESS) and lift peak
  SNR.

Sources: [Knapp & Carter 1976 (Semantic Scholar)](https://www.semanticscholar.org/paper/29c74aad1986ff2e907e084820e990a0544e743a) ·
[MathWorks gccphat](https://www.mathworks.com/help/phased/ref/gccphat.html) ·
[DSP Concepts GCC DOA](https://documentation.dspconcepts.com/awe-designer/8.D.2.6/generalized-cross-correlation-direction-of-arrival) ·
[MDPI/PMC SVD-GCC-PHAT (low-SNR)](https://pmc.ncbi.nlm.nih.gov/articles/PMC9571281/) ·
[Subsample TDE (ScienceDirect)](https://www.sciencedirect.com/science/article/abs/pii/S1051200406001230) ·
[CRLB for TDE](https://www.researchgate.net/publication/228896307)

### 4.4 Phone-mic feasibility & error budget
- **343 m/s ⇒ 1 ms ≈ 34 cm; 1 cm ≈ 29 µs.** Temperature scale error
  ~0.6 m/s/°C (≈0.9% per 5 °C) — negligible and common-mode.
- **Psychoacoustic target:** stereo lives in the **summing-localization**
  regime (< ~1 ms ICTD), where sub-ms timing *steers the phantom center*;
  at ~1 ms the image pulls fully to the lead speaker. So aim for a **small
  fraction of 1 ms** — sub-100 µs is comfortably good, ~0.3 ms is the edge
  of "noticeable". (Binaural ITD JND is ~10–20 µs; the speaker-pair
  trading ratio is coarser, but it sets the scale.)
- **The key design decision — capture both speakers in ONE recording.**
  Then the large, unknowable terms are **common-mode and cancel**: browser
  input latency (14–67+ ms, device-variable, and *unreportable* — Chrome's
  `MediaTrackSettings.latency` returns a constant 0.01), mic position, ADC
  group delay, bulk phone-clock drift over the window. What remains
  (the irreducible residual): the two speakers' DAC-latency *difference*
  (that's the signal you're measuring), relative DAC clock drift, capture
  jitter if arrivals aren't simultaneous, and differential reflections.
- **Disable `echoCancellation` / `autoGainControl` / `noiseSuppression`**
  in `getUserMedia` (all default `true`). Browser AEC actively warps the
  signal's time structure — fatal. (Precedent: `/balance/` already runs
  EC/NS/AGC off.)
- **First-wavefront windowing** rejects reflections: the direct path always
  arrives before any reflection, so window the correlation to the first few
  ms. Verify the chosen peak is the *earliest* strong one, not a louder
  reflection.
- **Keep the window short** (sub-second to ~2 s) to bound drift: ~50 ppm
  over 2 s ≈ 100 µs, so short windows keep drift in the tens of µs.
- **Realistic relative accuracy ≈ 10–50 µs** with EC/AGC/NS off + ESS +
  regularized GCC-PHAT + sub-sample interpolation + first-wavefront
  windowing + single short capture. That's ~1/20–1/100 of the full
  image-pull boundary — **~20× margin** over what imaging needs, so the
  repo's "p99 < 1 ms" working target is conservative. **Useless for
  *absolute* TOA** (browser latency) — the differential framing is the
  whole game.

Sources: [Speed of sound (Sengpiel)](https://sengpielaudio.com/calculator-speedsound.htm) ·
[Summing localization](https://en.wikipedia.org/wiki/Summing_localization) ·
[Precedence effect](https://en.wikipedia.org/wiki/Precedence_effect) ·
[ITD µs precision (PMC)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3704885/) ·
[MDN outputLatency](https://developer.mozilla.org/en-US/docs/Web/API/AudioContext/outputLatency) ·
[Jeff Kaufman — browser audio latency](https://jefftkaufman.substack.com/p/browser-audio-latency) ·
[getUserMedia audio constraints](https://medium.com/@haochuan/audio-constraints-in-getusermedia-9d3601a95893) ·
[ADC clock drift (ppm)](https://www.dsprelated.com/showthread/comp.dsp/32386-1.php) ·
[Aalto TDoA/GCC-PHAT](https://speechprocessingbook.aalto.fi/Enhancement/tdoa.html)

### 4.5 Snapcast latency control
- `Client.SetLatency` **confirmed**; per-client `client.config.latency`
  field; broadcasts `Client.OnLatencyChanged`.
- **Units = integer milliseconds** (confirmed via Snapweb UI `ms`
  adornment + snapclient manpage; `control.md` omits units).
- **No upstream clamp** — the field is an unbounded int. (A "±150 ms"
  range is a third-party UI artifact, *not* upstream Snapcast.)
- **Granularity is the real constraint:** whole-ms steps = ~34 cm each,
  coarse vs the sub-ms imaging target.
- Snapcast already clock-syncs clients to **~0.2 ms** (server timestamps
  chunks; clients continuously sync + buffer + add/drop samples).
  `SetLatency` corrects only what happens *after* the client hands samples
  to ALSA: **fixed per-device output-path/DAC/amp/codec latency** (its
  design intent — the canonical example is an HDMI soundbar). It has no
  notion of speaker geometry.
- **Conclusion:** carry the **fine acoustic alignment in the leader's
  sample-accurate CamillaDSP per-channel delay** (applied upstream of the
  Snapcast split, baked into the shared stereo stream — for a 2-speaker
  pair per-channel == per-endpoint). Reserve whole-ms Snapcast latency for
  the coarse fixed endpoint output-path offset. Sub-ms `SetLatency` is not
  needed. (The integer-ms limit only bites for >2 speakers / a sub, where
  a channel's fine offset can't ride the leader's stereo bake.)

Sources: [Snapcast JSON-RPC control.md](https://raw.githubusercontent.com/snapcast/snapcast/master/doc/json_rpc_api/control.md) ·
[Snapcast README (sync)](https://raw.githubusercontent.com/badaix/snapcast/master/README.md) ·
[snapclient(1)](https://manpages.debian.org/unstable/snapclient/snapclient.1.en.html) ·
[Snapweb Client.tsx](https://raw.githubusercontent.com/snapcast/snapweb/master/src/components/Client.tsx) ·
[Issue #886 (per-device latency)](https://github.com/snapcast/snapcast/issues/886)

### 4.6 Commercial prior art
- **No commercial smart speaker (Sonos, HomePod, Echo, Nest) publicly does
  mic-at-the-listening-seat inter-speaker arrival-time alignment.** They
  all do (a) per-speaker frequency/EQ room adaptation (Google: none) +
  (b) **network clock sync** between units, then **assume symmetric
  placement**.
- Sonos **Trueplay** = external phone-mic room **EQ/filtering** (not L/R
  timing). HomePod **room sensing** = *built-in* mics sensing *its own*
  reflections for EQ/beamforming (not the listener's seat). Echo Studio
  **"adaptive sound"** = undocumented built-in-mic filter adaptation.
  Stereo-pair coordination is network sync (Sonos ~1–2 ms; AirPlay 2;
  Cast; Alexa).
- **Implications:** (1) the feature is genuinely differentiated, and that's
  why there's no blueprint — REW is the only methodological precedent;
  (2) it earns its keep specifically in the **asymmetric-placement case**
  (unequal speaker-to-couch distances). Scope it as an **opt-in seat
  calibration**, not an always-on default; consider surfacing it only when
  a measured offset exceeds the ~0.3 ms "noticeable" threshold.

Sources: [Sonos Trueplay](https://support.sonos.com/en-ca/article/tune-your-sonos-speakers-with-trueplay) ·
[Apple HomePod newsroom](https://www.apple.com/newsroom/2023/01/apple-introduces-the-new-homepod-with-breakthrough-sound-and-intelligence/) ·
[Apple — HomePod stereo pair](https://support.apple.com/guide/homepod/set-up-a-homepod-stereo-pair-apd1ed62a52a/homepod) ·
[Google Nest pairing](https://support.google.com/googlenest/answer/7559493?hl=en) ·
[Echo Studio adaptive sound (review)](https://hometechnologyreview.com/amazon-echo-studio-smart-speaker-with-dolby-atmos-review/) ·
[US7805210 (network speaker sync)](https://patents.google.com/patent/US7805210)

---

## 5. Design recommendations for the sync flow

1. **Productionize Increment 4**, don't start fresh — build on
   `scripts/multiroom-spike-measure.py acoustic` and mirror `/balance/`'s
   privacy/measurement-window scaffolding (leader-only, EC/NS/AGC off,
   hearing-safe ceiling, measurement window held).
2. **Excitation = log sine sweep (ESS)** (upgrade from the throwaway
   click). Low crest factor → more energy under the safety ceiling;
   distortion windowed to negative time. Keep it as wideband as the phone
   mic / speaker allow.
3. **Estimator = regularized, SNR-/coherence-weighted GCC-PHAT** with
   **zero-pad sub-sample interpolation** (not plain xcorr + parabolic fit).
4. **Single short capture, one fixed mic, both L/R markers inside it** —
   the common-mode-cancellation invariant. Correlate the recording against
   each speaker's known reference.
5. **First-wavefront windowing** to reject reflections; verify the peak is
   the earliest strong one.
6. **Target sub-1 ms residual** (summing-localization regime). Apply the
   fine offset via the **leader's sample-accurate CamillaDSP per-channel
   delay**; reserve whole-ms Snapcast latency for the coarse fixed
   endpoint-path offset.
7. **Confidence gating:** repeat 3×, require SNR/repeatability before
   reporting or applying (as the proposal says).
8. **Scope as opt-in seat calibration** for asymmetric placement; surface
   conditionally when the measured offset exceeds ~0.3 ms.

---

## 6. Suggested sequencing (revised from the proposal)

1. **Decide what JTS4 is.** If a Zero 2 W endpoint: "trust balance on JTS4"
   is blocked on the endpoint install tier (Phase 2) landing, and endpoint
   trim there is `Client.SetVolume`, not outputd. If it runs the full
   stack: balance already works — live-test today.
2. **Add the doctor check** ("endpoint trim configured and active") — cheap,
   closes a documented-behavior-needs-a-test gap. For the endpoint tier the
   probe is "is the leader's per-client volume taper applied", not "is
   outputd's env set".
3. **Live-test `/balance/` on the full-speaker pair** (JTS + JTS3) now;
   defer JTS4 until #1.
4. **Productionize the sync flow** per §5 (ESS + regularized GCC-PHAT +
   single-capture + CamillaDSP delay), with the regression coverage the
   repo requires (a hardware-free analyzer test on a synthetic
   known-offset capture; doctor/`/state` surfaces for the applied delay).

---

## 7. Key code & doc references

- `jasper/web/balance_flow.py`, `jasper/multiroom/balance.py`,
  `deploy/assets/balance/js/main.js` — balance flow
- `jasper/web/rooms_setup.py` — manual trim
- `jasper/multiroom/{config,reconcile,state}.py` — grouping
- `jasper/multiroom/snapcast_rpc.py` — Snapcast JSON-RPC (extend for
  `Client.SetVolume` / `Client.SetLatency`)
- `rust/jasper-outputd/src/config.rs` — `dac_content` trim application
- `jasper/cli/doctor/grouping.py` — doctor checks
- `scripts/multiroom-spike-measure.py` — throwaway acoustic-sync reference
- `docs/HANDOFF-multiroom.md` — canonical multiroom design (Increment 4/5/6)
- `docs/dumb-endpoint-bringup.md` — Zero 2 W endpoint tier (Phases 1–4)

---

*Compiled 2026-06-13 from a codebase audit + seven web research passes.
Citation caveats from the research: Snapcast latency units/sign convention
are inferred from secondary sources (control.md omits them); Knapp & Carter
1976 is paywalled (claims via consistent summaries); commercial "computational
audio"/"adaptive sound" mechanisms are vendor marketing, not published
algorithms; some phone-mic magnitude estimates are reasoned from cited ppm /
latency / reverberation fundamentals rather than a single packaged source.*
