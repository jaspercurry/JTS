# Active DSP Commissioning for the JTS Two-Way Smart Speaker
## Speaker-Baseline Layer for Raspberry Pi 5 + CamillaDSP + Epique E150HE-44

**TL;DR**
- For a CamillaDSP-driven two-way with the Dayton Epique E150HE-44 + a typical 1″ horn-loaded compression tweeter, the strongly recommended default baseline is an **LR4 acoustic-target crossover in the 1.8–2.5 kHz band** — audioXpress measured the E150HE-44 as -3 dB at 30° off-axis at 2.8 kHz and recommends "a cross point in that vicinity or lower"; the on-axis +7 dB peak at 7 kHz mandates ≥24 dB/oct slope — implemented with per-driver IIR Biquad LinkwitzRiley filters, per-channel `Delay`/`Gain`/`Limiter` blocks, and convolution (`Conv`) reserved for either linear-phase crossovers or measured driver inverse-EQ.
- The speaker-baseline layer must be set anechoically/quasi-anechoically (gated REW measurements, VituixCAD merge of NF+FF+diffraction) and contains **only**: BSC, per-driver linearization, crossover, alignment delay, polarity, and per-driver brick-wall limiters. Room correction and preference/voicing EQ are downstream layers and must never be folded into the speaker baseline.
- Safe commissioning order: characterize each driver in-box at low SPL with a temporary protective high-pass on the tweeter; set acoustic-target crossovers in VituixCAD; transfer to a CamillaDSP YAML pipeline (Mixer → BSC/EQ on input pair → split → per-driver HP/LP + Delay + Gain + Limiter); verify summed response, vertical lobing, and step response by re-measurement before unlocking volume.

---

## Key Findings

### 1. Crossover topology — pick the alignment, not the electrical filter
- **What sums flat is the *acoustic* response, not the electrical filter.** The drivers' own roll-offs, baffle diffraction, and physical offset all shape the acoustic transfer function; the electrical filter is whatever you need to add to the raw driver response to land on the target. This is the central thesis of Linkwitz's 1976 AES paper "Active Crossover Networks for Noncoincident Drivers" (JAES 24(1), pp. 2–8): "Additional active delay networks are used to compensate for offsets in the acoustical planes from which the individual drivers radiate."
- **Linkwitz–Riley (even-order)** is the de-facto default. LR2/LR6 require one driver inverted; LR4/LR8 are non-inverting. LR legs are -6 dB at Fc and sum to a flat 0 dB on-axis (Wikipedia, *Linkwitz–Riley filter*: "the resulting Linkwitz–Riley filter has a −6 dB gain at the cut-off frequency. This means that when summing the low-pass and high-pass outputs, the gain at the crossover frequency is 0 dB.").
- **Butterworth (odd-order or 2nd-order non-LR)** is -3 dB at Fc per leg and sums to +3 dB on-axis at Fc — usually undesirable unless one is deliberately filling a hole.
- **Bessel** has the best transient/group-delay behavior but shallower in-band roll-off and worse out-of-band rejection — generally a poor fit for protecting a tweeter near its Fs.
- **Slope selection.** LR2 (12 dB/oct) has wide overlap, is lobing-prone, and requires polarity inversion; only OK when drivers are already well-behaved out of band. LR4 (24 dB/oct) is the modern default: clean lobe, no inversion, 360° phase rotation through the crossover (i.e., the woofer is one full cycle late at Fc, but that is broadband and inaudible per Linkwitz). LR8 (48 dB/oct) gives near-textbook driver isolation at the cost of more group-delay distortion in the crossover band; useful when the tweeter Fs is uncomfortably close to Fc or the woofer breakup is uncomfortably close above Fc.
- **Lobing.** Per Linkwitz 1976 and the Vanderkooy–Lipshitz JAES 34(4), 1986 follow-up ("Power Response of Loudspeakers with Noncoincident Drivers"), even-order non-LR (Butterworth) crossovers tilt the main lobe off-axis and add a peak; LR alignments keep the main lobe on the design axis. Steeper slopes (LR4/LR8) tighten the lobing region but make it narrower in frequency, which is generally desirable.

### 2. FIR vs IIR
- **IIR (Biquad LinkwitzRiley/Highpass/Lowpass in CamillaDSP)** is causal, low-latency (sub-millisecond at typical chunk sizes), CPU-cheap, with no pre-ringing. It has frequency-dependent group delay through the crossover region (90°/180°/360° per LR2/3/4). On a Raspberry Pi 5 it is essentially free — 4 channels of LR4 plus a handful of PEQs is well under 1% CPU.
- **FIR (Conv block)** can give arbitrary magnitude *and* phase response — most usefully a linear-phase crossover or driver-inverse-EQ where the driver's natural phase response is corrected. Costs are: (a) constant latency equal to half the filter length, e.g. a 16 k-tap FIR at 96 kHz adds ~85 ms each way — usable for music but not live monitoring; (b) potential **pre-ringing** if the magnitude target has sharp notches or the linear-phase crossover is very steep; mark100 on diyAudio writes plainly: "A big reason that I think NOT to use global FIR on top an existing speaker setup, be it passive or active, is to avoid pre-ring."; (c) requires that pre-ring artifacts stay below masking thresholds.
- **Recommendation for JTS baseline:** IIR LR4 for the crossover, IIR Peaking/Highshelf/Lowshelf biquads for per-driver linearization, and reserve `Conv` for an optional linear-phase global phase-fix at the end, generated by rePhase from the measured summed excess phase. The `wirrunna/CamillaDSP-Building-a-Config` repo on GitHub is the canonical recipe ("Extract a measurement of Excess Phase from a FS measurement and Export it for input to rePhase... manipulate the phase to get the Excess Phase close to zero").

### 3. Crossover-frequency selection for the JTS pair
- **Woofer (Dayton Epique E150HE-44, 5.5", DVC, Fs 40 Hz, Xmax 14.7 mm, 83.3 dB/2.83 V/m, dual 4 Ω):** audioXpress Test Bench measured "a fairly smooth rising response with no break-up modes or peaking out to about 5kHz, with a 7dB peak in the response at 7kHz, where it begins its low-pass roll-off" and explicitly recommends "A cross point in that vicinity [2.8 kHz] or lower should work well to achieve a good power response." Dayton's own spec sheet states usable range "30 – 4,000 Hz" and the cone is reinforced "to further control cone breakup and help maintain controlled off axis dispersion." Implication for JTS: the woofer is the limiting partner on the high side, not the tweeter. Target crossover ≤2.8 kHz; with LR4, expect the woofer to be 24+ dB down by 5.6 kHz, so the 7 kHz peak is at least ~30 dB suppressed.
- **Compression driver on horn:** typical 1" exit compression drivers have Fs in the 400–900 Hz band and manufacturer-recommended crossover at 1.2–1.5 kHz with steep slopes (LR4 or LR8). Per Steve Feinstein's editorial note in the Audioholics article "Do Better Parts Matter in Loudspeaker Crossovers?": "A basic, ages-old but still true, rule of thumb states that a designer is usually safe when he crosses a driver over at double its resonant frequency... Another good rule of thumb says, '18 dB down at resonance.'" With LR4 24 dB/oct you reach -18 dB at 1.4 octaves below Fc — i.e., Fc ≥ 1.4 × (2×Fs) is the conservative bound.
- **JTS starting point:** **2.0 kHz LR4 acoustic target** is the safe sweet spot: it gives the E150HE-44 plenty of margin below its 2.8 kHz directivity knee and below the 7 kHz peak, while keeping the tweeter at least 1.5–2× Fs for a typical horn-loaded compression driver. Refine after measurement; do not move down below 1.5 kHz without confirming tweeter HD < 1% at the target SPL.

### 4. Driver alignment — measure, don't guess
- Acoustic centers shift with frequency and depend on cone/dome geometry and horn depth. Purifi's tech-note "Time/Phase Alignment, Acoustic Center, Lobing etc." states explicitly: "Consequently, and contrary to common intuitive belief, vertical alignment of the voice coils does not in general result in the same latency time on axis. This often leads to an over-compensation of the latency e.g., when using a very stepped baffle." Physical voice-coil alignment is not the same as acoustic alignment, especially with a horn.
- Practical procedure (Rod Elliott, *Finding the Acoustic Centre of Loudspeakers*, sound-au.com): mic on axis between the drivers, drive each driver in turn with a pulse or sweep, measure time-of-flight from the electrical impulse to the acoustic arrival. Elliott measured offsets of 27–38 mm between a typical mid-bass and dome tweeter. With a horn-loaded compression driver the woofer is usually *ahead* in time because the horn adds path length, so the **delay typically goes on the woofer**, not the tweeter, in JTS.
- Polarity convention summarized by the Rane primer and the LR Wikipedia article: LR2 → invert one driver; LR4 → no inversion; LR6 → invert; LR8 → no invert. Always verify with a measurement: invert the tweeter and confirm the summed response shows a **deep null** centered at Fc; non-inverted should sum flat. If neither extreme gives a deep null, your alignment delay is wrong.
- Phase tracking through the crossover: in REW, overlay the two driver phase traces with the crossover *enabled*; they should lie on top of each other for at least an octave around Fc. The 0–360° offset added by LR4 must wrap identically on both legs.

### 5. Measurement workflow (REW + VituixCAD canonical, with open-source alternatives)
- **REW (free, JohnPM)** is the canonical capture tool. For each driver measured separately at 1 m on the intended axis, capture a log-sine sweep, identify the first reflection in the impulse-response window, and apply IR gating with a right-window before that reflection. Kimmo Saunisto's VituixCAD measurement guide (VituixCAD_Measurement_REW.pdf) recommends short windows (~3–5 ms) for tabletop quasi-anechoic captures, and explicitly cautions against single-channel-only timing normalization: "Single channel measurement systems such as USB mics with possible latency variations are not recommended for speaker engineering due to timing and phase variations and normalizations."
- **Near-field + far-field merge.** Measure the woofer near-field (5–10 mm from dust cap) to capture the bass roll-off below the gating cut-off, then merge to the far-field response with VituixCAD's Merger tool; include a baffle-step diffraction simulation. This is the only way to get a trustworthy speaker-baseline response below ~300 Hz indoors without a Klippel NFS.
- **VituixCAD** then does the crossover simulation: load merged FRD/ZMA per driver, simulate the IIR filters, and read on-axis sum, vertical/horizontal polars, listening-window average, and predicted in-room curve. This catches lobing errors before you ever load YAML into CamillaDSP.
- **Open-source / Python alternatives.** ARTA (free for non-commercial; closed-source), Audacity for raw sweeps, `pyfar` (https://pyfar.org) for impulse-response handling and filter design in Python, `python-soundfile` for IO. None match the workflow polish of REW+VituixCAD, but they are scriptable and good for a guided-commissioning tool.
- **Klippel-style methodology** (NFS, anechoic-equivalent far-field synthesis from near-field spherical scan) is the gold standard but requires the hardware. The DIY takeaway is the methodology: separate driver response from room contribution.

### 6. Safe individual driver measurement
- **Tweeter protection first.** Before any sweep that goes below the tweeter's safe range, insert a protective high-pass (a Biquad Highpass at ~2× planned Fc, Q 0.707, e.g. 4 kHz HP if planning a 2 kHz crossover) in the CamillaDSP pipeline for the duration of characterization. Sweep from above the safe HP frequency, capture, then if you need the response below that, drop the HP to (say) 1.5× Fs and sweep at very low level only briefly.
- **Sweep type.** Log-sine sweep (ESS, exponential / "Farina sweep" from Farina's seminal AES paper "Simultaneous Measurement of Impulse Response and Distortion With a Swept-Sine Technique") is the practical standard. Exponential sweep gives equal energy per octave (gentler on the tweeter than linear sweeps), better SNR than MLS, and separates the linear impulse response from harmonic distortion components — meaning you can read THD vs frequency from a single sweep. Refined in Farina's 2007 AES 122nd Convention paper "Advancements in Impulse Response Measurements by Sine Sweeps." REW implements this directly.
- **Sweep level.** For unprotected raw-tweeter sweeps, practitioner consensus (Parts-Express TechTalk: "I measure 0.5v output at the amplifier terminals with my multimeter and run a full sweep. Loud enough to get decent measurements without risking tweeter failure") is to keep amp output around 0.5 V at the terminals, start the sweep above Fs, and ramp up cautiously while monitoring HD live. The audioXpress/Voice Coil Test Bench standard for *characterizing* drivers including tweeters is 94 dB SPL @ 1 m for distortion sweeps ("the SPL set to 94dB (my criteria for home audio transducers) at 1m (16V), using a SoundCheck pink noise stimulus") — but this is for already-protected drivers. Erin's Audio Corner uses 76 dB @ 1 m as the reference level specifically for its dynamic-compression sweep: "a 2.7 second logarithmic sine sweep referenced to 76dB at 1 meter" (with the FR itself captured on a Klippel NFS and distortion sweeps referenced to 96 dB equivalent). **JTS recommended commissioning sweep level: ~80 dB @ 1 m, with HD monitoring; escalate to 90 dB @ 1 m only after the protective HP is in place.**
- **DC and turn-on protection.** Many high-Fs compression drivers can be killed by amp turn-on thumps or accidentally-output sub-bass DC. Beyond CamillaDSP `Limiter` filters, the conservative practice from Lenard Audio (*Horns: Compression Drivers*): "Protection capacitors must be placed in series between the amplifier and compression driver or compression tweeter. The turn on-off pulses and DC offset from the amplifier can easily destroy the diaphragms." For an all-digital path with a Class-D module and an always-on tweeter-channel HP in DSP, a series film cap sized to cross *well* below the planned acoustic Fc (e.g., a passive HP at ~300 Hz, well below the DSP HP at 2 kHz) is a cheap, effective belt-and-suspenders measure.
- **Limiter strategy.** In CamillaDSP 2.x/3.x, the `Limiter` is a per-channel **peak** limiter (confirmed by author Henrik Enquist on diyAudio: "Is the limiter a peak limiter ... Yes") with parameters `clip_limit` (dB FS) and optional `soft_clip: true/false`. There is no attack/release — those belong to the separate `Compressor` *processor*. The official README documents the underlying clip semantics (in the Compressor section but the Limiter reuses the same logic): "clip_limit: the level in dB to clip at. Providing a value enables clipping of the signal... soft_clip: enable soft clipping. Set to false to use hard clipping... Note that soft clipping introduces some harmonic distortion to the signal." Example YAML:
  ```yaml
  filters:
    tweeter_limit:
      type: Limiter
      parameters:
        clip_limit: -6.0
        soft_clip: true
  pipeline:
    - type: Filter
      channels: [2, 3]      # tweeter L/R
      names: [tweeter_limit]
  ```
  Set the tweeter `clip_limit` such that, after per-driver gain trim and crossover gain, full-scale digital + reference room gain stays below the tweeter's continuous thermal rating with margin. Default starting point: `clip_limit: -3 dB` on the tweeter channel with soft-clip enabled, `clip_limit: 0 dB` on the woofer channel.

### 7. Separating speaker correction from room correction and preference voicing
This is the most important architectural decision in the project. The three layers must be physically distinct pipeline stages and tagged accordingly:

| Layer | What it does | Where measured | Where it lives in the CamillaDSP pipeline |
|-------|--------------|----------------|--------------------------------------------|
| **A. Speaker baseline** (this research) | Per-driver linearization, BSC, acoustic-target crossover, polarity, delay, per-driver limiters | Quasi-anechoic gated (REW + VituixCAD merge), no room contribution | Per-channel **after** the 2→4 Mixer; outputs are "speaker-flat" on the design axis |
| **B. Room correction** | Modal-region EQ (typically <500 Hz), spatially-averaged listening-window EQ above the Schroeder frequency | At listening position(s), spatial average | Applied to the **stereo input pair** before the Mixer split; never per-driver |
| **C. Preference / voicing** | Harman/Toole/Olive in-room slope (gentle downward tilt), user bass/treble trims | Subjective + Olive's published target curves | Applied on the **stereo input pair**, after room correction, last in the chain |

Why this separation matters: collapsing them is the most common DIY error and the reason "smart" room-correction products often sound worse than no correction. Toole's 2015 JAES paper "The Measurement and Calibration of Sound Reproducing Systems" makes the case explicit — the *direct sound* (= layer A) should be flat / smooth; the *steady-state in-room response* (= what layers B+C produce) should slope downward. If you bake the slope into layer A, you end up with a speaker that measures wrong anechoically and that any future room correction will fight.

Olive's published listener-preference research (Olive, S.E., "The Perception and Measurement of Headphone Sound Quality," *Acoustics Today*, Vol. 18, Issue 1, Spring 2022) identifies three statistically distinct preference clusters: Class 1 ("Harman Target Lovers") at 64% of listeners; Class 2 ("More Bass Is Better"), 15%, "Harman target with 4-6 dB more bass"; Class 3 ("Less Bass Is Better"), 21%, who "prefer the Harman target curve with 2 dB less bass." JTS's preference layer should expose a parametric tilt control plus a low-shelf, not bake in a single curve.

**Baffle step compensation lives in layer A**, applied to the *stereo* signal pair *before* the Mixer split, because the diffraction loss is the same on both drivers (Henrik Enquist's step-by-step CamillaDSP guide does it this way: `bafflestep` highshelf in front of the 2→4 mixer). Magnitude: 4–6 dB total, transition centered on f₃ = 115/W (m), per John Murphy's formula cited by Rod Elliott (sound-au.com); verify by quasi-anechoic measurement and dial in by ear only with the room layer disabled.

### 8. CamillaDSP architecture for the JTS pipeline
Recommended pipeline (top to bottom = signal flow):

```yaml
devices:
  samplerate: 96000          # 96k is a good balance; 48k also fine
  chunksize: 1024            # ~10 ms latency at 96k; FFT-friendly
  capture: { type: Alsa, channels: 2, device: "hw:Loopback,1,0", format: S32LE }
  playback: { type: Alsa, channels: 4, device: "hw:<DAC>", format: S32LE }

mixers:
  to4chan:
    description: "Stereo input → 4 channels: 0/1 = woofer L/R, 2/3 = tweeter L/R"
    channels: { in: 2, out: 4 }
    mapping:
      - { dest: 0, sources: [{ channel: 0, gain: 0, inverted: false }] }
      - { dest: 1, sources: [{ channel: 1, gain: 0, inverted: false }] }
      - { dest: 2, sources: [{ channel: 0, gain: 0, inverted: false }] }
      - { dest: 3, sources: [{ channel: 1, gain: 0, inverted: false }] }

filters:
  # --- pre-split (stereo) ---
  bafflestep:     { type: Biquad, parameters: { type: Highshelf, freq: 450, slope: 6.0, gain: -4.0 } }
  # --- woofer ---
  lp_2k_lr4:      { type: BiquadCombo, parameters: { type: LinkwitzRileyLowpass, freq: 2000, order: 4 } }
  woofer_eq_1:    { type: Biquad, parameters: { type: Peaking, freq: 250, q: 2.0, gain: -2.0 } }   # example
  woofer_delay:   { type: Delay,  parameters: { delay: 0.10, unit: ms } }                          # if horn is recessed
  woofer_trim:    { type: Gain,   parameters: { gain: 0.0 } }
  woofer_limit:   { type: Limiter, parameters: { clip_limit: 0.0, soft_clip: true } }
  # --- tweeter ---
  hp_2k_lr4:      { type: BiquadCombo, parameters: { type: LinkwitzRileyHighpass, freq: 2000, order: 4 } }
  tweeter_eq_1:   { type: Biquad, parameters: { type: Peaking, freq: 5000, q: 3.0, gain: -3.0 } }  # example horn dip
  tweeter_trim:   { type: Gain,   parameters: { gain: -5.0 } }    # match sensitivity to woofer
  tweeter_limit:  { type: Limiter, parameters: { clip_limit: -3.0, soft_clip: true } }

pipeline:
  # Stage 1: pre-split corrections (apply to both input channels)
  - { type: Filter, channels: [0, 1], names: [bafflestep] }
  # Stage 2: split to 4
  - { type: Mixer,  name: to4chan }
  # Stage 3: woofer chain
  - { type: Filter, channels: [0, 1], names: [lp_2k_lr4, woofer_eq_1, woofer_delay, woofer_trim, woofer_limit] }
  # Stage 4: tweeter chain
  - { type: Filter, channels: [2, 3], names: [hp_2k_lr4, tweeter_eq_1, tweeter_trim, tweeter_limit] }
```

Ordering notes:
- Per-driver EQ comes **after** the crossover filter so the linearization shapes only the in-band response, not the rejected band.
- `Delay` is best placed mid-chain (after EQ, before gain trim) so latency-dependent measurements remain stable across config edits.
- `Limiter` is **last** in each per-driver chain so it sees the actual signal headed for the DAC including all gain and EQ-induced boosts.
- Polarity inversion (e.g., for LR2): use `inverted: true` on the tweeter source in the `to4chan` Mixer rather than a negative `Gain` — clearer in the YAML and survives reload.
- For linear-phase variant: replace `lp_2k_lr4`/`hp_2k_lr4` with `type: Conv` blocks loading rePhase-generated coefficient files; expect ~50–100 ms total latency and watch for pre-ring on the summed step response.
- TDM with 4× PCM5102A is supported through ALSA as a 4-channel/8-channel device; the Mixer's `out: 4` matches whatever ALSA reports as the channel count.

Sample-rate strategy: keep CamillaDSP at the DAC's native rate (96 kHz typical for PCM5102A). The Pi 5 can also run 192 kHz comfortably for short FIR loads, but the marginal benefit for crossover IIRs is zero.

### 9. Visualizations the commissioning tool needs
Build these *in order* — each gates the next:
1. **Raw driver SPL + phase** per driver, gated, with the protective high-pass annotated.
2. **Impedance + Fs + Qts** per driver (DATS or measured at the amp terminals), to predict thermal/excursion limits.
3. **Filtered driver SPL + phase** with the current CamillaDSP crossover applied, overlaid on the raw — confirms the electrical filter delivers the intended acoustic target.
4. **Driver-to-driver phase tracking** through the crossover region (one octave each side of Fc), with delay/polarity controls live-linked.
5. **Summed acoustic response** (on-axis), with deep-null test (invert tweeter) overlay.
6. **Vertical polar at ±15°, ±30°** to confirm lobing direction is symmetric and on-axis.
7. **Step response + group delay + excess group delay** of the sum. Look for: smooth step with one dominant arrival (woofer follows tweeter for non-inverted LR4); excess GD < 1 ms across 200 Hz–8 kHz.
8. **Predicted spinorama / DI** from VituixCAD if a full polar set was captured.
9. **Per-driver limiter headroom meter** showing current peak/average and clip_limit margin in real time via CamillaDSP's websocket API.

### 10. Prior art & ecosystem
- **VituixCAD** (Kimmo Saunisto, free, closed-source) — best simulator+merger. Direct REW workflow documented at https://kimmosaunisto.net/Software/VituixCAD/VituixCAD_Measurement_REW.pdf.
- **REW** (John Mulcahy, free) — universal capture/IR/window/EQ tool. https://www.roomeqwizard.com.
- **rePhase** (free, https://rephase.org) — FIR generation for linear-phase crossovers and global phase fixes. Pairs naturally with CamillaDSP's `Conv` block.
- **DRC-FIR** (Denis Sbragion) — open-source FIR room correction; useful for layer B, not for layer A.
- **Acourate** and **Audiolense** — commercial; high-quality FIR generation for layer B with some layer-A capability.
- **HouseCurve, FuzzMeasure** — mobile/Mac measurement; weaker than REW for crossover work but fine for spot checks.
- **CamillaDSP ecosystem:** `pyCamillaDSP` (Python control), `pyCamillaDSP-plot` (filter plotting), `camillagui` (web GUI). The `wirrunna/CamillaDSP-Building-a-Config` repo on GitHub is the canonical step-by-step for an active multi-way with measurement-driven gain/delay/phase-fix using REW + rePhase. `mdsimon2/RPi-CamillaDSP` documents the Raspberry-Pi-specific Alsa/Loopback setup.
- **Hypex Filter Design** — closed-source but free for Hypex hardware; useful as a reference UI for what an active-speaker tuning tool looks like (15 biquads/channel, IIR+FIR, presets, soft-clip limiter).
- **miniDSP plug-ins** — closed-source; commercial UI patterns to study.
- **Academic anchors:**
  - Linkwitz, S.H., "Active Crossover Networks for Noncoincident Drivers," JAES 24(1), pp. 2–8 (Feb 1976). https://aes2.org/publications/elibrary-page/?id=2649
  - Lipshitz, S.P., Vanderkooy, J., "A Family of Linear-Phase Crossover Networks of High Slope Derived by Time Delay," JAES 31, pp. 2–20 (Jan/Feb 1983).
  - Vanderkooy, J., Lipshitz, S.P., "Power Response of Loudspeakers with Noncoincident Drivers — The Influence of Crossover Design," JAES 34(4), pp. 236–244 (Apr 1986).
  - Farina, A., "Simultaneous Measurement of Impulse Response and Distortion with a Swept-Sine Technique," AES 108th Convention (Feb 2000); refined in AES 122nd Convention (May 2007).
  - Toole, F.E., "The Measurement and Calibration of Sound Reproducing Systems," JAES 63(7/8) (Jul/Aug 2015). PDF at https://www.linkwitzlab.com/Toole-Room%20calibration.pdf
  - Toole, F.E., *Sound Reproduction* (Routledge), 4th edition.
  - Olive, S.E., "The Perception and Measurement of Headphone Sound Quality," *Acoustics Today* Vol. 18, Issue 1 (Spring 2022).
  - Linkwitz Lab archive at https://www.linkwitzlab.com — design philosophy and crossover papers.

---

## Staged Implementation Ladder for JTS

**Milestone 0 — Bench prerequisites**
- DATS or impedance jig; measured Re, Le, Fs, Qts for each driver as built into the enclosure (not free-air).
- CamillaDSP "flat" pipeline confirmed working from 2-ch source → 4-ch DAC → 4 amp channels → drivers, with a temporary tweeter HP at 4 kHz LR4 already in place.
- REW + UMIK calibration files loaded (use the correct 0° or 90° file for your orientation); loopback path for 2-channel timing reference established.

**Milestone 1 — Raw driver characterization (in-box, no crossover)**
- Mount the speaker on a stand outdoors or on a tall table with all reflective surfaces > 1.2 m away.
- Per driver, with the *other* driver muted in the Mixer:
  - Capture far-field (1 m, on intended listening axis) gated log-sine sweep at ~0.5 V amp terminal → ~80 dB @ 1 m.
  - Capture near-field at 5–10 mm from dust cap (woofer) and at the horn mouth (tweeter).
  - Tweeter: keep protective HP active; only sweep above the HP corner.
- Export FRD/IR per driver; in VituixCAD Merger combine NF+FF+diffraction simulation to get a baseline anechoic-equivalent response.
- Plot impedance phase to extract acoustic Fs and confirm the Xmax-limited region.

**Milestone 2 — Acoustic-target crossover design (simulation only)**
- In VituixCAD: load merged FRD/ZMA per driver, simulate LR4 at 2.0 kHz, add per-driver PEQs to land on a smooth combined target (Toole-style flat direct sound).
- Iterate: check vertical lobing at ±15°, ±30°, sum-flat (non-inverted), deep-null (one inverted).
- Resolve the driver Z-offset by entering measured time-of-flight differences and confirm the simulated phase-tracking aligns.
- Lock the design: export target slopes, Fc, per-driver gain, polarity, delay (in samples at 96 kHz), and biquad list.

**Milestone 3 — Transfer to CamillaDSP**
- Build the YAML pipeline per the architecture above; populate every filter with the VituixCAD-derived values.
- Reload via websocket; confirm pipeline is valid via `camilladsp -c config.yml --check`.
- Set conservative limiters: tweeter `clip_limit: -6 dB soft_clip: true`, woofer `clip_limit: -3 dB soft_clip: true`. Tighten later after thermal validation.

**Milestone 4 — Verification (re-measurement)**
- Repeat the gated 1 m measurement with the full crossover active. Overlay on the VituixCAD prediction. Tolerance: ±1 dB through the crossover region; phase tracking within ±20° one octave each side of Fc.
- Deep-null test: invert tweeter via the Mixer, capture, confirm > 20 dB null centered at Fc. If null shifts ±1/3 octave from Fc, adjust delay.
- Step response: confirm single coherent leading edge (or two consistent edges with known offset for non-time-corrected designs).
- Polar check at ±15° and ±30° vertical — confirm symmetric lobing (no tilt toward ceiling/floor unless deliberate).

**Milestone 5 — Limiter/thermal commissioning**
- Sweep at progressively higher levels (5 dB steps) up to the intended max SPL. Monitor woofer cone excursion visually for ≤ 70% Xmax (≤10 mm peak for the E150HE-44) and tweeter HD via REW; back off whenever HD > 3% in the tweeter band.
- Set final `clip_limit` values such that limiter activation precedes any audible distortion or thermal stress, with ~3 dB margin.

**Milestone 6 — Baseline freeze**
- Lock the YAML, version it, tag it as `baseline-v1.yml`.
- This is the input to the (separate) room-correction layer. Layers B and C live in a different YAML or a clearly-tagged section appended to the pipeline.

---

## Recommended Safe Defaults for the JTS Active 2-Way

| Parameter | Value | Rationale |
|---|---|---|
| Crossover topology | **LR4 (24 dB/oct) acoustic target** | De-facto modern default; no polarity flip; clean lobe; good tweeter protection. |
| Starting Fc | **2.0 kHz** | Below E150HE-44 directivity knee at 2.8 kHz and well below the 7 kHz peak; above 2× Fs of any reasonable horn compression driver. |
| Tweeter polarity | **Non-inverted** (LR4 sums in-phase) | Verified by deep-null test on inversion. |
| BSC | **Highshelf, freq f₃ = 115/W (m), slope 6 dB/oct, gain −3 to −5 dB** | Murphy formula; trim to taste in-room only with the other layers off. |
| Per-driver delay | **Measured ToF difference** (typ. 0.05–0.30 ms on the woofer in a horn+cone build) | Horn extends acoustic path, so woofer usually gets the delay. |
| Tweeter sensitivity match | **Gain trim −3 to −15 dB on tweeter channel** | Professional 1″ compression drivers commonly span 107–114 dB/W/m (e.g., BMS 4540ND rated 114 dB @ 1 W/1 m; Faital Pro HF108 rated 109 dB @ 1 W/1 m), while home-audio 1″ tweeters run ~95–98 dB (e.g., Dayton ND25FA ~96 dB). E150HE-44 is 83 dB/2.83V — expect a 12–30 dB gap to a pro compression driver. |
| Sweep level (commissioning) | **~80 dB @ 1 m**, escalate to 90 dB only after protective HP in place | Practitioner consensus; preserves headroom and tweeter safety. |
| Sweep type | **Log-sine (ESS/Farina)**, 256k samples, 1 s minimum | Farina 2000/2007; separates linear IR from HD. |
| Tweeter limiter | **`Limiter clip_limit: -3 to -6 dB, soft_clip: true`** | CamillaDSP peak limiter, per-channel; matches Hypex active-speaker norms. |
| Woofer limiter | **`Limiter clip_limit: 0 dB, soft_clip: true`** | Protects DAC/amp clipping; excursion limit is the dominant constraint, not thermal. |
| Sample rate | **96 kHz / S32LE / chunksize 1024** | Pi 5 has bandwidth; FFT-friendly chunk; ~10 ms latency. |
| Optional FIR | **Linear-phase global phase fix via rePhase + `Conv`**, applied last on the stereo input pair | Optional polish; latency budget ~50–100 ms. |
| Series cap on tweeter (physical) | **Yes, sized for ~300 Hz HP** | Belt-and-suspenders against amp turn-on DC. |

---

## Failure Modes and Guardrails

**Things that blow a compression driver / horn tweeter**
1. **Amp turn-on/off DC thump while DSP pipeline is muted or rebooting.** CamillaDSP's mute and reload are graceful, but the audio interface and amps may not be — and a Pi 5 reboot cycles through a window where DSP isn't running but the amp is on. Mitigate with: amp soft-start, a physical series cap (~300 Hz HP), or amp standby triggered after DSP is up.
2. **Sweeping below Fs without a protective high-pass.** A 1 s log sweep at full system gain through a tweeter's 700 Hz Fs will dissipate ~10× the rated power as the impedance peak dumps power into the voice coil. Always run the protective HP during commissioning.
3. **Removing the crossover by editing only the LP on the woofer.** Editing the YAML while CamillaDSP is running is fine, but if you bypass the tweeter HP for "comparison," do it through a Mixer mute, not by deleting the filter.
4. **Sub-bass DC content in source material** (e.g., warped vinyl, infrasonic plops). The pre-split stage should be preceded by a stereo IIR HP at ~20–30 Hz to discard DC and rumble.
5. **Limiter set too high, soft_clip off, full-scale 1 kHz sine source.** Soft-clip on, conservative limits, and a tested fail-safe (mute on websocket-disconnect) are all important.

**Things that produce misleading measurements**
1. **Single-channel timing normalization.** REW with a USB UMIK alone strips absolute timing; you cannot align drivers from a single-channel capture. Use a 2-channel sound-card setup with an electrical loopback for the reference, or capture both drivers in a single sweep with both channels active.
2. **Too-short gating window** — strips low frequencies. Saunisto recommends quoting the lower frequency limit as 2× (1 / window length).
3. **Calibration file missing or mismatched.** UMIK-1 has *two* calibration files (0° and 90°); using the wrong one above 5 kHz tilts the tweeter response.
4. **Measuring at the design *vertical* axis instead of the design *listening* axis.** Most horn+cone 2-ways have a vertical null below the on-axis listening point; measure where you will actually sit.
5. **Forgetting to re-measure after polarity flips or delay edits** and trusting the simulator.
6. **Confusing the +7 kHz peak of the E150HE-44 with cone breakup.** audioXpress explicitly notes this is the natural low-pass roll-off knee, not a breakup mode; no notch filter needed if Fc ≤ 2.5 kHz with LR4.

**Common DIY mistakes**
1. Baking the in-room slope (Harman/Olive) into the speaker baseline. Don't. Keep layers A/B/C separate.
2. Using the woofer's voice-coil center as the acoustic-center reference. Per Purifi, this over-compensates for horn-loaded systems.
3. Picking a crossover frequency based on the tweeter alone, ignoring the woofer's directivity narrowing.
4. Using LR2 because it's "musical" without inverting one driver — produces a deep, room-mode-like dip at Fc.
5. Comparing simulated and measured responses without applying identical smoothing (1/12 vs 1/3 octave) — apparent dips can be artifacts of smoothing mismatch.

---

## Caveats

- The JTS-specific crossover frequency recommendation (2.0 kHz LR4) is based on the *measured* E150HE-44 behavior (audioXpress) and a *generic* 1" compression driver. Substituting a smaller-format or shallower-horn tweeter, especially one with Fs > 900 Hz, may push the safe Fc up to 2.5 kHz; the workflow in Milestones 1–4 handles this automatically.
- The CamillaDSP `Limiter` is a peak limiter only; it is not a thermal model. Tweeter thermal protection still depends on conservative `clip_limit` values and physical headroom, not on limiter activation history.
- The Pi 5's audio-side stability with 8-channel USB DACs or 4× PCM5102A in TDM is workload-dependent; verify no underruns at chunksize 1024 / 96 kHz under realistic load before locking values.
- Speaker-baseline measurements are only as anechoic as your gating and merge let them be; below ~250 Hz, baseline EQ is partly faith and partly diffraction-simulator output. Don't EQ aggressively below the merge crossover.
- Olive's preference-target work is well-validated for the *headphone* domain and well-supported for the *speaker* domain in typical listening rooms, but it is statistical: 36% of listeners prefer something other than the central target (15% want +4–6 dB more bass, 21% want −2 dB less bass per Olive 2022). JTS should expose voicing as a user-adjustable layer, not bake one curve in.
- I could not locate the exact CHANGELOG entry stating which CamillaDSP version first added the standalone `Limiter` *filter* (as distinct from the `Compressor` *processor* in v1.1.0). The Limiter filter is present in v2.0.x and v3.x documentation; if you are on an older release, verify before relying on it.