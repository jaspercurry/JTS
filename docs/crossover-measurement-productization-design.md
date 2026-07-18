# Crossover measurement & tuning — productization design

> **Status: design proposal / decision record (2026-07-18). Not yet
> implemented.** Motivated by the end-to-end hardware-validation run
> ([crossover-room-e2e-validation-log.md](crossover-room-e2e-validation-log.md)),
> which surfaced concrete level-management and phase/delay-alignment friction on
> real hardware, and by a first-principles deep-research pass (Appendix A, with
> the brief that produced it in Appendix B). This doc is the *synthesized
> decision record*: the framework, the resolved tradeoffs, the staged plan, and
> the empirical gaps we must close before building the hard part. It does **not**
> restate the canonical measurement/correction references
> ([docs/HANDOFF-audio-measurement-core.md](HANDOFF-audio-measurement-core.md),
> [docs/HANDOFF-correction.md](HANDOFF-correction.md)); it proposes how the
> `/correction/crossover/` measurement flow should adapt.

## 1. Context & problem

JTS calibrates a **fully active, bi-amped 2-way crossover** (separate DAC
channels → woofer + tweeter; no passive network — the DSP crossover *is* the
sonic crossover and the tweeter's only protection). The measurement/tuning flow
is browser-driven: the mic is a calibrated UMIK-2 on the **user's** phone/laptop,
captured via `getUserMedia`, E2E-encrypted, relayed to the speaker, which pulls
the recording back. Target user is a non-expert; north star is *"place the mic
roughly where we suggest, tap through a few prompts, and the speaker ends up
correctly aligned and flat."*

The 2026-07-18 validation run drove the whole flow on JTS3 (Dayton E150HE-44
woofer + B&C DE250 horn tweeter, LR4 @ 2 kHz) and exposed two real problems:

- **Level management ("too loud").** With the mic near-field (~3 cm), the
  driver **sweep** repeatedly read "too loud for the microphone at this distance"
  and the flow re-leveled quieter, iterating. Observed directly: the single-tone
  level lock *passed*, then the full-band sweep clipped on the woofer's efficient
  band.
- **Phase/delay alignment** is the step we least understand how to productize on
  a phone mic, because of the constraint below.

## 2. The constraint that shapes everything

**There is no hardware loopback and no sample-accurate timing between playback
(on the speaker) and capture (on the user's phone).** Absolute latency is
unknown, large, and varies per session (OS audio stack + USB/BT + browser
buffering); the speaker DAC clock and phone ADC clock are independent and can
drift. Therefore **a driver's *absolute* acoustic delay is unrecoverable** from
an isolated capture. Every timing/phase method must be robust to this.

Two implementation facts (verified against our architecture, not just theory):
the **relay does not break single-capture timing** — it transports a *complete
continuous WAV*, not a real-time stream — and the **tweeter safety gating**
between excitations is band-limiting/level-capping, not a timing break.

## 3. First-principles framework (three models)

**Level / capture.** Excite with an exponential (log) sine sweep + Farina
deconvolution (pink spectrum → SNR where it's worst; harmonic products window
out; low ~4 dB crest factor). Set gain by **predicting the sweep's peak dBFS**
from driver sensitivity + the target filter + mic sensitivity, confirmed by one
low-level **pilot sweep** — *not* by a single steady tone (a tone sets gain by
one frequency; the sweep peaks wherever the driver+DSP response peaks). A capture
that is **hot but not clipped is fully valid** (linear gain preserves
magnitude+phase); accept-and-normalize, retry only on true clipping. Set the
window dynamically from the measured ambient floor (target SNR above it, ~6 dB
guard below full scale).

**Driver measurement.** Deconvolve the sweep → impulse response → window out room
reflections → FFT → **complex transfer function**, mic-cal applied. Near-field
isolates the driver, but 3 cm is ergonomically hostile (mm-per-dB; a 108 dB horn
overloads a phone ADC). Prescribe a more forgiving **~10–20 cm** on-axis
(still quasi-near-field at 2 kHz for these drivers; recover room-immunity by
windowing), reserving true near-field only for an optional deep-bass splice.

**Crossover alignment.** Summed pressure `P(f) = W(f) + T(f)` (complex). Flat sum
⇒ level match + correct relative polarity + **time alignment** (`τ = Δd/c`;
phase error `= 2πfτ`). Recover the *relative* level/polarity/delay from **one
continuous recording containing BOTH drivers** (sequential, closely-spaced
chirps): the common unknown latency **cancels in the arrival-time difference**.
Delay via cross-correlation + sub-sample (parabolic / GCC-PHAT) interpolation;
**geometry (path difference) is a prior/sanity-check, not the source of truth**;
validate with a reverse-null / summed-flatness check at Fc. **Minimum-phase is a
scalpel:** magnitude→phase for EQ + the woofer, but the inter-driver acoustic
offset is *excess* phase (measure it) and the horn tweeter isn't minimum-phase
near cutoff.

## 4. Decisions (resolved tradeoffs)

| Decision | Resolution | Why |
|---|---|---|
| Leveling reference | Sensitivity-model + crest budget (predict) **+ pilot sweep** (confirm); retire single-tone lock | Only a full-band probe sees the sweep's true peak bin |
| Hot captures | **Accept-and-normalize**; retry only on true clip/overload | Linear gain preserves magnitude & phase (REW aborts only >30% clipped) |
| Distance | **Fixed ~10–20 cm prescribed**, level **adaptive** (distance, sensitivity, mic id, ambient floor) | A fixed distance is a simple repeatable instruction; level must adapt or we iterate |
| Near vs far field | Two-tier: quasi-near-field per driver (windowed) for raw response; far-field/listening for the room pass | Robustness for a shaky hand beats textbook near-field purity we can't exploit on a phone |
| Relative phase/delay | **Single-capture, both drivers** (primary) → latency-immune; summed-response/reverse-null (validate); geometry (prior) | Only single-capture cancels the unknown, drifting latency |
| Magnitude vs phase | Magnitude+phase; minimum-phase **only** for EQ/woofer; measured excess phase for the inter-driver offset; horn phase measured near cutoff | The whole point of alignment is excess phase |
| Repeats / cadence | **Auto-run** the stationary repeats (clean-capture selection); no per-repeat tap; do **not** average across drifting clocks | Fewer taps; avoids the per-repeat 120 s-window friction; averaging on async clocks combs |
| `getUserMedia` hygiene | Request AGC/EC/NS **off** and **verify via `getSettings()`**; refuse/warn if not honored | Browser AGC silently rewrites levels; EC/NS destroy magnitude & phase |

## 5. Staged adaptation plan (mapped to our surfaces)

This is **adaptation, not a rewrite** — the flow already has near-field sweeps, a
geometry attestation, summed capture, and a polarity/delay candidate search. We
retarget them.

**Stage 1 — cheap, high-leverage, ship first (mostly parameter/logic):**
- Predicted gain from a sensitivity+crest model + a low-level pilot sweep
  (owns: the level-match / `calibration_level` window + the sweep level).
- Accept-and-normalize hot-but-unclipped captures (owns: the sweep-result gate).
- Auto-run the stationary repeats with clean-capture selection — **removes the
  per-repeat tap + 120 s window that stalled the validation run**.
- Prescribe ~10–20 cm; make the target level adaptive.
- Enforce + verify `getUserMedia` constraints at session start.
- Pre-fill the driver/target profile for known hardware.

**Stage 2 — the single-capture-both-drivers aligner (new subsystem, the core IP):**
- One continuous recording, both drivers, closely spaced; deconvolve to two IRs
  on the shared ADC timeline; cross-correlate + sub-sample for the relative
  delay; drift-sanity-check; geometry prior. Replaces the alignment step's source
  of truth (geometry-attestation + summed-search → single-capture primary,
  geometry as prior).
- Summed-response / reverse-null validator with a one-glance before/after.

**Stage 3 — robustness/polish:** blind clock-drift-ratio estimation (relax the
chirp-gap constraint), optional near-field bass-extension splice, room pass
(multi-position, 90° cal).

## 6. Empirical-validation gaps (close before building Stage 2)

The framework's two load-bearing claims are, by its own admission, **derivations,
not measurements on our stack**:

1. **The `<0.4 s` chirp-gap clock-drift budget** assumes our phones are ≤50 ppm.
   A cheap phone on a ceramic resonator can be ±5000 ppm — at which point the
   aligner would silently emit a garbage delay. This is a *correctness/safety*
   gap. **Measure our device population's actual drift vs JTS3.**
2. **"Two sequential chirps share one latency"** is sound in theory but
   unvalidated on our `getUserMedia → E2E-relay → Pi-pulls-WAV` path. **Prototype
   the single-capture relative delay on real hardware and check repeatability.**
3. **Horn-tweeter minimum-phase validity near cutoff** — treat as measured until
   proven.

We have the exact rig (UMIK-2 + JTS3 + the browser flow) to close (1) and (2)
directly; do this before committing Stage 2 code. Benchmarks to graduate:
median captures-per-driver ≤ 1.2 with zero silent AGC corruption (Stage 1);
relative-delay repeatability within ±1 sample (±20.8 µs @ 48 kHz) over 10
sessions and summed crossover ripple ≤ ±1.5 dB through Fc (Stage 2).

## 7. Prior art (what transfers)

REW's **acoustic timing reference** (one HF chirp anchors time for all sweeps in
one session) is the direct precedent for single-capture relative timing without
loopback; REW also disables multi-sweep averaging for USB mics and aborts only on
>30% clipping. Dirac Live/DLBC derive delay/phase from the sweeps + a timing
sweep. VituixCAD/ARTA's dual-channel + **excess-group-delay** method is our
alignment estimator. Trinnov triangulates with a 4-capsule mic (geometry
massively simplifies alignment — we substitute the single-capture trick +
optional user geometry). Bryan/Kolar/Abel (AES 2010) and Gamper (HSCMA 2017) are
the citable core for clock-drift-robust deconvolution. Full citations in
Appendix A.

## 8. Primary sources

- **Appendix A** — full first-principles deep-research report (verbatim).
- **Appendix B** — the research brief that produced it.
- The e2e run that motivated this:
  [crossover-room-e2e-validation-log.md](crossover-room-e2e-validation-log.md).

---

## Appendix A — deep-research report (primary source)

The full first-principles report — reasoning, worked clock-drift budget, and the
complete citation list (Keele JAES 1974; Farina log-sine; Bryan/Kolar/Abel AES
2010 Paper 8169; Gamper HSCMA 2017 + public MATLAB; REW acoustic-timing-reference
+ USB-mic guidance; VituixCAD excess-group-delay; TI SLLA122 ±50/±100 ppm) — is
preserved verbatim in
[crossover-measurement-deep-research-2026-07-18.md](crossover-measurement-deep-research-2026-07-18.md).
Section 3–7 above are the *authoritative synthesis* for our decisions; the report
is the reasoning behind them.

## Appendix B — research brief

The brief that produced the report (product context, the no-loopback constraint,
the per-step flow + UX + first-principles math, the tradeoffs, and the ask) is
archived at `deep-research-crossover-measurement-prompt.md` (session artifact;
available on request to add in-repo).

---

_Last updated: 2026-07-18 (design proposal; pre-implementation)._
