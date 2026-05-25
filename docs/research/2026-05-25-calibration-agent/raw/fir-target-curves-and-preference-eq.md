# JTS Research Corpus — Room Correction, FIR, Target Curves, and Preference EQ

## Executive Summary

This corpus consolidates the technical and perceptual literature relevant to the next stages of JTS — an open-source Raspberry Pi 5 smart speaker built on CamillaDSP. The consensus across primary sources (Toole, Olive, Welti, Geddes, Sbragion, Enquist, Mulcahy) is unambiguous on several points:

1. **Below the Schroeder/transition frequency (~200–300 Hz in domestic rooms, per Toole), EQ is justified, effective, and largely position-dependent. Above it, EQ should be limited to correcting the loudspeaker's own (minimum-phase) anomalies, not the room's interference patterns.** Aggressive correction above transition de-correlates stereo image, smears transients, and destroys the very direct-sound balance the listener evaluates timbre with.
2. **The "correct" in-room target is not flat.** It is a smoothly downward-tilted curve whose slope depends on speaker directivity and room reflectivity. Toole presents a *band* of acceptable curves; community shorthand of "−1 dB/octave" is convenient but not Toole's own number.
3. **FIR's unique value over IIR is phase/time-domain correction and arbitrary magnitude shapes**, but for the JTS use-case (non-pristine room, phone-mic measurement, smart-speaker form factor) the audible benefit ladder is steep: minimum-phase IIR PEQ in the modal region delivers the vast majority of the value; FIR adds the remainder only when measurement quality, mic calibration, and the speaker's own phase response are good enough to justify it.
4. **Pre-ringing in linear-phase FIR is real but largely avoidable** with frequency-dependent windowing (FDW), psychoacoustic smoothing, and cuts-biased target shapes — exactly the discipline DRC-FIR, Acourate, Audiolense, and rePhase enforce.
5. **A Raspberry Pi 5 has more than enough headroom** for stereo FIR room correction. The CamillaDSP README documents the Pi 4 doing 8-channel × 262 144-tap × 192 kHz convolution at ~55% CPU; the Raspberry Pi Foundation's own benchmarks (Oct 2023) put the Pi 5 at "a 2–3× increase in CPU performance relative to Raspberry Pi 4" thanks to the Cortex-A76 at 2.4 GHz. Tap counts up to ~131k @ 48 kHz stereo are trivially feasible.
6. **JTS should architecturally separate (a) physical/objective room correction from (b) subjective preference EQ**, chain them as distinct filter banks in CamillaDSP, and reserve the LLM "audio engineer" layer to *advise on strategy and translate user language* — never to perform the math or override safety limits.

The rest of this document is structured as: source ranking → six topical sections → consensus / debate / recommendations / glossary / mapping table / proposed JTS markdown corpus outline.

---

## Source Quality Ranking

### Primary (peer-reviewed / first-party technical)
- **Floyd E. Toole**, *Sound Reproduction: The Acoustics and Psychoacoustics of Loudspeakers and Rooms* (Routledge, 3rd ed. 2018) — definitive reference.
- **Toole, F. E.**, "The Measurement and Calibration of Sound Reproducing Systems," *JAES* 63(7/8), 2015. PDF mirrored at https://www.linkwitzlab.com/Toole-Room%20calibration.pdf.
- **Olive, S. E., Welti, T., Khonsaripour, O.**, "A Statistical Model that Predicts Listeners' Preference Ratings of … Headphones — Parts 1 & 2," AES 143rd Convention, 2017; and Olive, "The Perception and Measurement of Headphone Sound Quality," *Acoustics Today* 18(1), Spring 2022. https://acousticstoday.org/wp-content/uploads/2022/03/The-Perception-and-Measurement-of-Headphone-Sound-Quality-What-Do-Listeners-Prefer-Sean-E.-Olive.pdf
- **Olive, S. E.**, "A Subjective and Objective Evaluation of Six Room Correction Products," AES Conv. Paper 7960 (2009). https://www.aes.org/e-lib/browse.cfm?elib=15154
- **Welti, T.**, "How Many Subwoofers Are Enough?" AES Conv. Paper 5602 (2002). https://www.aes.org/e-lib/download.cfm?ID=11355
- **Welti, T. & Devantier, A.**, "Low-Frequency Optimization Using Multiple Subwoofers," *JAES* 54(5):347–364 (2006). PDF: https://audioroundtable.com/misc/Welti_Multisub.pdf
- **Schroeder, M. R.**, "Frequency-correlation functions of frequency responses in rooms," *JASA* 1962 (the original transition-frequency paper).
- **Dirac Research**, "On Room Correction and Equalization of Sound Systems" (white paper by Mathias Johansson). https://www.dirac.com/wp-content/uploads/2021/09/On-equalization-filters.pdf
- **Sbragion, D.**, DRC-FIR documentation: https://drc-fir.sourceforge.net/doc/drc.html
- **Raspberry Pi Foundation**, "Benchmarking Raspberry Pi 5" (Oct 2023). https://www.raspberrypi.com/news/benchmarking-raspberry-pi-5/

### Strong secondary (first-party technical that isn't peer-reviewed; manufacturer engineers; experienced practitioners)
- **Brüggemann, U.**, Acourate documentation & wiki (https://www.audiovero.de/acourate-wiki/).
- **Mulcahy, J.**, REW Help & forum (https://www.roomeqwizard.com/).
- **HEnquist (Henrik Enquist)**, CamillaDSP README and CHANGELOG (https://github.com/HEnquist/camilladsp).
- **Toole, F.**, columns at audioholics.com (his own venue).
- **Olive, S.**, *seanolive.blogspot.com* (the author's own blog).
- **Sound on Sound** review of Genelec GLM 4.2 (https://www.soundonsound.com/reviews/genelec-glm-42).
- **AudioXpress / Vance Dickason**, "Trouble with the Curve" (interview with Toole). https://audioxpress.com/article/trouble-with-the-curve
- **Sonarworks research blog**, "Flat is the predominant choice for sound production" (Feb 2022). Cited for first-party usage statistics.

### Weaker community / hobbyist (useful for workflow, not for claims)
- diyAudio forum threads (rePhase, CamillaDSP, REW-as-FIR-maker).
- Archimago's Musings (informed hobbyist measurements).
- AVS Forum, AVNirvana, hometheatershack.com (workflow questions).
- Vendor marketing pages (Dirac, Audyssey, Sonarworks) — used only when corroborated.

---

## 1. FIR Room Correction Fundamentals

### What FIR adds over IIR/PEQ
IIR biquads are minimum-phase by construction (after cascading 2nd-order sections): a magnitude change is always accompanied by a corresponding minimum-phase shift. This is *correct* when the system being corrected is itself minimum-phase — which low-frequency room modes largely are at a single position. For arbitrary magnitude shapes, phase linearisation, time-domain alignment between drivers or subs, brick-wall crossovers, and non-minimum-phase correction (e.g. genuine non-causal energy in the impulse response from delayed reflections), only FIR can do the job. The Dirac white paper makes the point bluntly: "an FIR part is required in order to do correction of the phase/impulse response properties of an acoustic system" (https://www.dirac.com/wp-content/uploads/2021/09/On-equalization-filters.pdf).

### Minimum-phase vs linear-phase vs mixed-phase
- **Minimum-phase FIR/IIR**: phase tied to magnitude; no pre-ringing; correct choice when the underlying anomaly is minimum-phase (most isolated room modes at one mic position are). REW's automatic EQ generator outputs minimum-phase biquads for this reason.
- **Linear-phase FIR**: symmetric impulse response, constant group delay, but *pre-ringing* equal to post-ringing for any non-flat magnitude shape. Pre-ringing is the price of perfect phase.
- **Mixed-phase**: separates the response into a minimum-phase part and an "excess-phase" part. Only mixed-phase filters can actually *reduce* the time-domain ringing of a non-minimum-phase system (the room). Dirac, Acourate (via excess-phase decomposition), and Audiolense use this approach. Quote from the Dirac paper: "minimum-phase and linear-phase room correction filters can't physically optimise the acoustic impulse response in a room. At best, they can minimise problems caused by the application of a filter."

### Audibility caveats
The community-empirical consensus from Archimago's blind tests, ASR/Gearspace technical threads, and the bodziosoftware pre-ringing paper (http://www.bodziosoftware.com.au/Pre_Ringing.pdf) is:
- Pre-ringing is **audible** for sharp brick-wall filters and high-Q linear-phase boosts. The bodziosoftware paper concludes: "Rough safety limits according to both test methods would be to keep the order of a linear phase FIR crossover filter under 600 at higher frequencies (1 and 3 kHz) to prevent from the ringing phenomenon producing audible errors."
- Pre-ringing is **inaudible / beneficial** when used to compensate a real post-echo of the system (e.g. a speaker's HP rolloff). rePhase author "pos": "Where filters are used to compensate a natural post-echo, such as that of the low frequency roll-off of a loudspeaker, pre-echo is advantageous with no detrimental audible effects."
- For smooth EQ shapes with modest gain, pre-ringing is generally below threshold (Dirac's claim, broadly accepted).

### Frequency-dependent windowing (FDW)
FDW gates the impulse response with a window whose length is a fixed number of cycles at each frequency rather than fixed in seconds. At low frequencies the window is long (many ms → captures modal behaviour); at high frequencies the window is short (few ms → captures direct sound only, ignores reflections). It's the keystone trick in Acourate (https://www.audiovero.de/acourate-wiki/doku.php?id=en:wiki:funktionen:td-functions:frequency_dependent_window) and DRC-FIR (Sbragion). Default windows are commonly 15/15 cycles (pre/post peak) in Acourate.

Sbragion's DRC documentation goes further, comparing FDW resolution to the ERB psychoacoustic scale: "the correction resolution used by DRC is well above that of any of the standard smoothing procedures, at least with the normal.drc sample settings file. This means that the correction should provide a perceived frequency response that is really close to the configured target frequency response."

This is why FDW dominates: it matches the human auditory time–frequency resolution, naturally produces a "perceived flat" target above transition while preserving the speaker's direct sound, and avoids the snake-oil trap of correcting room reflections that are part of the room's signature, not the speaker's.

### Tap count, latency, CPU, Pi 5 budget
**Verified primary-source benchmark (CamillaDSP README at https://github.com/HEnquist/camilladsp):** "A Raspberry Pi 4 doing FIR filtering of 8 channels, with 262k taps per channel, at 192 kHz. CPU usage about 55%." CamillaDSP uses partitioned (segmented) FFT convolution where FFT length = 2 × chunksize, and segment count = ceil(filter_length / chunksize). The author's own statement: "Using a smaller chunk size (i.e. more segments) reduces latency but makes the convolution process less efficient. Although a smaller chunk size leads to increased CPU usage for all filters, the difference is larger for FIR filters than the other types."

Per the Raspberry Pi Foundation's own benchmark blog (Oct 2023), "Raspberry Pi 5 delivers a 2–3× increase in CPU performance relative to Raspberry Pi 4" — courtesy of the Cortex-A76 cores at 2.4 GHz.

Practical Pi-CamillaDSP latency numbers from the community:
- mdsimon2 (diyAudio CamillaDSP thread): "At 48 kHz I've been using 128 chunk size, 384 target level and 2 second adjust period … Latency is around 15 ms which is not bad at all and fine for an audio/video application."
- mdsimon2's RPi-CamillaDSP tutorial: default chunksize 1024 with target level 3× → **~25 ms** latency.
- General diyAudio Pi 5 user report: 256 chunksize → ~50 ms (older buffer-of-4× setting).

For JTS at 48 kHz stereo, **tap counts up to ~65k–131k are realistic** (covers down to ~1 Hz frequency resolution; far more than needed). A practical cap of **16 384 taps** gives ~340 ms of impulse response — more than enough for full-range FDW correction — and uses negligible Pi 5 CPU. Build CamillaDSP with appropriate `RUSTFLAGS` to enable NEON for additional speedup on Pi.

### When FIR is genuinely useful — and when it isn't
- **Useful**: time-aligning multiple drivers; phase-linearising crossovers; correcting a speaker's own anechoic response; FDW-based bass correction; mixed-phase modal-decay reduction at a single seat.
- **Snake oil / harmful**: linear-phase EQ above the transition frequency that "fixes" the in-room steady-state response by correcting reflections (which are not minimum-phase); deep-boost FIR cuts/peaks (pre-ringing audible); single-position high-resolution correction over a wide listening area; correcting nulls below 30 Hz with large boosts (drives amp/driver into distortion long before the dip flattens).

---

## 2. Room Correction Limits

### Why EQ cannot fix narrow nulls and SBIR
A null is a physical cancellation (two paths of equal amplitude, opposite phase) at one location. EQ can only scale the signal sent to the speaker; it cannot change the path-difference geometry. Boosting at the null frequency just makes the speaker work harder while the cancellation persists. **SBIR** (speaker-boundary interference response) — the comb pattern caused by reflection from a nearby wall — is the same: position-dependent and not fixable by EQ. Toole's well-known position is that the only real fixes are: move the speaker, move the listener, or absorb the offending boundary.

### Why multi-position spatial averaging matters
A single-point measurement captures one specific interference pattern. Audyssey's Chris Kyriakakis (https://www.audioholics.com/room-acoustics/audyssey-room-eq-interview) puts the point as: "It is fairly simple to show that measuring in one microphone location and creating a filter that 'corrects' for that tiny spot will lead to poor equalization results. The room correction filters must be informed about acoustical problems throughout the listening area … Audyssey MultEQ collects the information from multiple measurements and then creates groups (clusters) among them based on the similarity of the problems found." Toole's recommendation (echoed by every major room correction product) is to measure several positions in the listening area and average, weighted toward the MLP.

The **moving-microphone method (MMM)** — slowly waving the mic over a 0.5–1 m volume around the listening position while accumulating an RTA with periodic pink noise — gives a spatial average that correlates well with Harman's predicted in-room (PIR) curve (Loudspeakers.audio: "MMM is very comparable to Harman's In-Room or CEA-2034 prediction based on anechoic measurements"). MMM trades phase/time information for excellent steady-state spatial accuracy and is by far the most JTS-appropriate method for a non-expert user with a phone.

### What should and should not be corrected above transition
Toole (Audioholics quote): "In domestic rooms it [Schroeder] is around 200-300 Hz. Below that frequency, the room dominates the quality of sound because of resonances. Above that frequency it is the combination of the loudspeaker axial frequency response and directivity, and reflectivity at the points of early reflections … that are principal determinants." Above transition, the listener's auditory system perceptually separates direct sound from reflections; "correcting" the steady-state in-room response globally breaks this separation. Olive's 2009 room-correction shootout (Paper 7960) found that the worst-rated commercial products were those that aggressively shaped HF response.

### Distinguishing modes / direct response / early reflections / artefacts
- **Room modes**: visible as peaks/nulls below ~300 Hz; long decay (>200 ms) in waterfall plots; position-dependent.
- **Direct response**: visible in the first ~3–5 ms of the impulse response (windowed/gated measurement). The only thing that should generally be EQ'd above transition.
- **Early reflections**: 5–30 ms after direct; visible in ETC (energy-time curve) plots; treated acoustically, not via EQ.
- **Measurement artefacts**: mic noise floor; phone-mic resonances; HVAC/traffic interference; clock drift between source and capture.

### Bass-only vs full-range correction
The defensible practice — and the one consistent with both Toole/Olive and the DRC/Acourate philosophy — is:
- **Below transition (~20–300 Hz)**: full PEQ or FIR correction toward a chosen target. Cuts preferred over boosts.
- **Transition to ~1 kHz**: gentle correction of broad trends only; FDW with long-ish window.
- **Above ~1 kHz**: leave the speaker's anechoic response alone; only correct measurable, repeatable speaker anomalies (e.g. driver resonance). FDW with short window (a few cycles).

### Subwoofer placement, multiple subs, treatment vs DSP
Welti's full 2002 conclusion (AES Paper 5602, abstract): "It was concluded that four subwoofers are enough to get the best results, but that two subwoofers located at wall midpoints are nearly as good as four, and also provide very good low frequency support." His later paper with Devantier (JAES 2006) introduced the Mean Spatial Variance (MSV) metric and identified Toole's "¼-room" placement as marginally better than wall-midpoints. Geddes' alternative: asymmetric random placement of multiple subs with overlapping mains, leveraging mode density rather than symmetry. Either is markedly more effective than any EQ-only solution because they attack the spatial-variance problem directly. JTS, as a single-box smart speaker, can't do multi-sub, but it can document the limitation honestly and recommend acoustic placement first, EQ second.

---

## 3. Target / House Curves

### The Harman / Olive-Welti research
The "Harman curve" for loudspeakers in rooms is the *measured average* of an anechoically-flat, controlled-directivity speaker (e.g. Revel Salon) in Harman's reference listening room. It has natural in-room downward tilt arising from speaker directivity (DI rising with frequency → less reflected energy at HF) and room absorption.

Olive's 2018 headphone-preference cluster analysis (https://acousticstoday.org/wp-content/uploads/2022/03/The-Perception-and-Measurement-of-Headphone-Sound-Quality-What-Do-Listeners-Prefer-Sean-E.-Olive.pdf):
- **Class 1 "Harman Target Lovers": ~64%** of listeners preferred the Harman target.
- **Class 2 "More Bass Is Better": ~15%** preferred ~3–6 dB more bass below 300 Hz (skews young, male).
- **Class 3 "Less Bass Is Better": ~21%** preferred ~2–3 dB less bass (skews older, female).
- Demographic split: 69% of males vs 56% of females are in Class 1.

These are *headphone* results but the qualitative pattern (majority prefer the reference; minority clusters want more or less bass; treble preferences are tighter) is widely consistent with loudspeaker preference literature.

### B&K curve
Brüel & Kjær Application Note 17-197 (1974) — the original "B&K listening curve" derived from preferred response in domestic listening rooms. Digitised form: +3 dB at 50–80 Hz, flat to ~200 Hz, then gentle downward slope reaching about −3 dB at 20 kHz. Strikingly similar in shape to the modern Harman in-room target despite a half-century gap. The slope from 200 Hz to 20 kHz averages roughly **−0.5 dB/octave**.

### Downward slopes — what numbers are actually defensible?
Toole presents a **family** of target curves (not a single slope). Community values quoted:
- **B&K**: ~−0.5 dB/octave overall, +3 dB shelf below 100 Hz.
- **"Harman in-room"** as drawn in *Sound Reproduction*: about −0.8 to −1.0 dB/octave from 100 Hz to 10 kHz, with a bass shelf of +2 to +4 dB.
- **VituixCAD design range**: −0.8 to −1.2 dB/octave is commonly recommended.
- **Bob Katz**: flat to 1 kHz, then straight line to −6 dB at 20 kHz.

The **sensitivity** finding (multiple diyAudio practitioners and Toole): 0.1 dB/octave change in slope is audibly significant for tonal balance.

### Why flat in-room sounds too bright
Loudspeakers with flat anechoic response radiate roughly omnidirectionally at low frequencies and more directionally at high frequencies (rising DI). A "flat" *direct sound* therefore yields a *downward-tilted in-room steady-state* response. If you EQ the in-room curve back to flat, you've forced the direct sound to rise with frequency — i.e. brightened it. Toole's Audio*Xpress* interview phrases it as understated: "too bright" is "a serious understatement." Olive's blog confirms (http://seanolive.blogspot.com/2009/11/subjective-and-objective-evaluation-of.html): "A flat in-room target response is clearly not the optimal target curve for room equalization. The preferred room corrections have a target response that has a smooth downward slope with increasing frequency. This tells us that listeners prefer a certain amount of natural room gain."

### Target relation to directivity and room absorption
- **High-DI speakers** (horns, line arrays) need a *flatter* in-room target (less room sound contributing).
- **Low-DI speakers** (omnis, dipoles) need a *steeper* tilt (more room contribution).
- **Dead rooms** (heavy absorption) need a flatter target; **lively rooms** need a steeper one.

Olive himself: "the optimal in-room target curve may depend on the loudspeaker directivity and reflectivity of the listening room. If the room is acoustically dead with few reflections and/or the directivity of the loudspeaker is quite high, the in-room response will represent a higher proportion of the direct sound, which should be flat."

### How much taste can override "reference"
Olive's preference clusters show roughly 36% of listeners do not prefer the Harman target as-drawn. Notably, Sonarworks' February 2022 research-blog post "Flat is the predominant choice for sound production" reports that "in absolute majority or 73% of cases the flat target is the preferred choice" across more than 67 000 calibrated speaker setups in their user base. The honest reading: the *flat-after-calibration* target is the modal choice in pro studios, but a substantial minority adjust it; that fraction matches Olive's roughly one-third out-of-Class-1.

Treat the reference target as the *default*, and offer a **bass shelf control (±6 dB, 20–250 Hz)** and **tilt control (±3 dB total slope across the spectrum)** as the primary preference levers — these correspond directly to the dimensions where measured listener variance is highest.

---

## 4. Preference EQ / Subjective Language Mapping

The subjective-descriptor literature converges (Audio University, Mastering The Mix, hearing-aid research at PMC10916511 which identified seven music-quality dimensions including *clarity*, *harshness*, *bass strength*, *treble strength*) on a fairly stable frequency taxonomy. Cross-referenced with Toole's discussion of timbre and Olive's preference clusters, the safe mapping is:

| User term | Likely cause | Frequency range | Suggested action | Clarifying question |
|---|---|---|---|---|
| "More bass" | Low shelf preference (~15% of listeners per Olive 2018) | 20–120 Hz | Low shelf +2 to +4 dB, corner ~120 Hz, Q=0.7 | "Across all music or specific genres? Below 100 Hz or 'punch' around 80–150 Hz?" |
| "Less boomy" | Modal peak or room gain excess | 60–200 Hz | Identify peak in measurement; apply narrow PEQ cut (Q 4–8, max −6 dB) at the peak; or low shelf −1 to −3 dB at 150 Hz | "Boomy on bass notes or on male vocals?" (separates 80–150 Hz from 200–400 Hz) |
| "Brighter" | Tilt preference; treble shelf | 4–16 kHz | High shelf +1 to +3 dB at 4 kHz, OR reduce overall downward tilt by 0.2 dB/octave | "More 'air' (>10 kHz) or more 'detail' (4–8 kHz)?" |
| "Warmer" | Too little 100–300 Hz, or too much 4–8 kHz | 100–300 Hz boost, OR 4–8 kHz cut | +1 to +2 dB shelf at 200 Hz, Q=0.7; or −1 dB at 5 kHz | "Lacking body, or too 'edgy' on vocals?" |
| "Vocals recessed" | Cut/dip in presence region | 1–4 kHz | Gentle +1 to +2 dB broad peak at 2–3 kHz, Q=1 | "Male or female vocals? Are sibilants too soft, or are vocals just behind the band?" |
| "Harsh" | 2–5 kHz excess (Toole's "presence" region) | 2–6 kHz | −1 to −3 dB broad cut, Q=1, centre 3–4 kHz | "Harsh on cymbals, vocal sibilance, or violins?" |
| "Thin" | Inadequate 80–250 Hz | 80–250 Hz | Low shelf +1 to +3 dB at 200 Hz; or check sub crossover | "Lacking weight on kick drum or on vocal body?" |
| "Muddy" | Excess 200–500 Hz | 200–500 Hz | −1 to −3 dB broad cut at 300 Hz, Q=1 | "Muddy on bass/drums or on vocals?" |
| "Too much treble" | Tilt/shelf | 6–16 kHz | High shelf −1 to −3 dB at 6 kHz; OR increase overall tilt 0.2 dB/octave | "Painful on cymbals, or just generally bright?" |

References for the mappings: Audio University's frequency-zone taxonomy (https://audiouniversityonline.com/describing-sound-quality/), Mastering The Mix's zone breakdown, the hearing-aid music-quality dimension study (https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10916511/) which validated *clarity / harshness / bass strength / middle strength / treble strength* as orthogonal user descriptors, and Toole's *Sound Reproduction* chapter on tonal balance.

---

## 5. Prior Art and Open-Source Tools

### REW (Room EQ Wizard, John Mulcahy)
The de facto open ecosystem. Strengths: free, mature, supports sweep + MMM, exports IR as WAV, FDW analysis, generates IIR biquad sets, vector averages, FIR generation via "EQ Filters → Export Filter Impulse Response." Limitations: built-in EQ generator outputs **minimum-phase only**; "true" linear-phase FIRs require the rePhase pipeline. REW stores impulse responses with 1 s of zero-padding before the peak — a known gotcha for downstream tools (causes channel mis-alignment in DRC-FIR unless the impulse is recentred).

### rePhase (pos / Thomas)
Free Windows tool to generate FIR coefficients (https://rephase.org/). Feature list (from the tool's site): "generate impulse responses (FIR) for convolution engines … loudspeakers phase linearization … linear-phase and minimum-phase EQ and shelving … multiple gain EQ algorithms (constant Q, proportional Q, constant shape, raised cosine, etc.) … arbitrary slopes linear-phase filters (Linkwitz-Riley, Brickwall, Horbach-Keele, etc.) … multiple windowing algorithms (Hamming, Hann, Blackman, Albrecht, etc.)." Workflow: measure in REW → export as text → load into rePhase → design corrections → generate IR (control "centering" mid/int) → load as `Conv` filter in CamillaDSP.

### CamillaDSP (Henrik Enquist)
Cross-platform Rust DSP engine. Both IIR (`Biquad`, `BiquadCombo`, `DiffEq`) and FIR (`Conv`) filters, mixers, gain/delay, dither, compressor. YAML config (the JTS native filter format). Critical engineering facts:
- Convolution via FFT, FFT length = 2 × chunksize, segmented when filter length > chunksize.
- Recent additions in CHANGELOG: `GraphicEqualizer`, `Tilt`, statefile for runtime persistence, multiple volume control faders.
- BiquadCombo offers convenient compound filters; for room correction these are ideal because they're parametrised by user-friendly numbers (frequency, gain, Q) rather than raw a/b coefficients.

A "CamillaFIR" project does **not** exist as a distinct tool — community references to "CamillaFIR" appear to mean either CamillaDSP's `Conv` filter or the `camillagui` editor.

### DRC-FIR (Denis Sbragion)
The seminal open-source room correction tool (https://drc-fir.sourceforge.net/doc/drc.html). Approach: psychoacoustic-resolution FDW + Toeplitz inversion + minimum-phase OR mixed-phase output. The DRC documentation contains the most technically rigorous open-source treatment of FDW resolution vs ERB scale. Sbragion's stated philosophy (paraphrased in the Alan Jordan DRCDesigner help): "DRC hasn't been designed to provide a 'desired frequency response' but to provide the most accurate reproduction of what's on the recording … This means that there's only one correct target: flat … this becomes 'perceived as flat'." The "Digital Room Correction Designer" by Alan Jordan wraps DRC with a GUI, sweep generation, target-curve drawing, and emits filters at four "strengths" (minimum-phase → linear-phase).

### Acourate (Uli Brüggemann, Audiovero)
Commercial Windows tool that pioneered FDW for room correction. Approach: log-sweep capture → FDW preparation (default 15/15 cycles) → target curve design → "Room Macro 3" inversion → optional excess-phase correction → outputs WAV convolution kernels. Distinguishing features: digital crossover generation, driver linearisation, time alignment. Mitch Barnett's *Accurate Sound Reproduction Using DSP* (book) is essentially the practitioner manual.

### Audiolense (Juice HiFi, Bernt Rønningsbakk)
Similar commercial workflow; emphasises full-system correction including phase and time alignment between drivers. Uses mixed-phase correction.

### Dirac Live
Proprietary mixed-phase correction. Per Dirac's own white paper, uses combined IIR+FIR structure for efficiency, applies look-ahead buffer for non-causal (excess-phase) inversion, supports per-listener-area "imaging" optimisation rather than single-point. Marketing claims of imaging/clarity improvement should not be taken at face value; the underlying mathematics (mixed-phase, multi-position averaging) is sound.

### Genelec GLM / SAM
Single-vendor ecosystem (Genelec smart monitors with onboard DSP). Calibration mic feeds GLM software, which generates IIR-based room compensation, applies bass management, level/distance alignment. Now includes "GRADE Room Acoustic Reports." Philosophy: known speaker → tightly-targeted correction → no global magnitude/phase tinkering. Quality of result depends heavily on speaker baseline.

### Neumann MA 1
Similar concept to GLM but with one notable claim: MA 1 applies model-specific phase corrections for Neumann KH-series monitors. This works because Neumann knows the crossover phase response of their own speakers. The implication for JTS: when the speaker is *known* (and JTS controls the hardware), per-speaker baseline calibration is a force multiplier.

### Sonarworks SoundID Reference
Per-pair (and per-headphone) profile-based calibration. For speakers: in-room sweep + flat target with optional shelves and tilt. Their published target options: Flat (SR), Dolby Atmos Music, Custom (parametric), Translation Check. Their own usage data — "Flat is the predominant choice for sound production" (Feb 2022) — reports that "in absolute majority or 73% of cases the flat target is the preferred choice" across more than 67 000 calibrated studio speaker setups; the remainder add custom shelves or tilts.

### Audyssey MultEQ XT32
Multi-position (up to 8 mic positions) measurement, clustering algorithm to spatially average without cancelling peaks-against-dips. XT32 has 32× the filter resolution of base MultEQ. Notable design idea relevant to JTS: clustering rather than naive averaging avoids the "peak + null = looks flat" trap.

### HouseCurve (iOS app)
Lightweight measurement + EQ-suggestion app. Notable for JTS-style UX: built for phone users, generates target curves and PEQ recommendations directly. The UX lesson is that non-experts will use what runs on their phone without extra hardware.

### Anti-Mode (DSPeaker), Trinnov Optimizer, Anthem ARC Genesis
- **Anti-Mode**: real-time IIR-based modal correction in sub-200-Hz range; design idea: target the bass region only, leave the rest alone.
- **Trinnov**: mixed-phase, multi-mic (3D mic array) correction; expensive but technically aggressive.
- **ARC Genesis**: per-speaker correction within Anthem AVRs.

---

## 6. Recommended JTS Design Implications

### What a JTS session bundle should store NOW
Every measurement session should persist enough raw data that *any* future correction algorithm (FIR, FDW, mixed-phase, AI-guided) can be derived without re-measurement. Recommended bundle contents:

```
session_<timestamp>/
  manifest.yaml              # schema version, timestamp, JTS firmware, CamillaDSP version
  sweep_config.yaml          # ESS parameters: f1, f2, T, level, sample rate
  sweep_recording.wav        # raw mic capture (32-bit float, full duration)
  impulse_response.wav       # deconvolved IR (32-bit float, 1 s pre-peak per REW convention)
  measurements/
    pos_01.wav, pos_02.wav…  # multi-position IRs if collected
    mmm_average.wav          # MMM-averaged response if used
  complex_fr.txt             # freq, magnitude, phase (REW text export format)
  spatial_average.txt        # vector-averaged complex FR
  mic_metadata.json          # phone model, OS, mic API used, browser, sample rate
  mic_calibration.txt        # if user uploaded a calibration file (UMIK-1 etc.)
  environment.json           # noise floor measurement, room dimensions if entered
  speaker_profile.json       # JTS hardware revision, known anechoic baseline
  applied_filters/
    bass_peq.yaml            # current CamillaDSP bass PEQ
    fir_correction.wav       # generated FIR if any
    preference_eq.yaml       # separate preference layer
  target_curve.txt           # the target this session corrected toward
  audit_log.json             # every decision the system/LLM made, with reasons
```

Storing the raw sweep recording (not just the deconvolved IR) is the single most important forward-compatibility decision: if future algorithms want to apply better windowing, alternative deconvolution, harmonic distortion analysis, or non-causal correction, they can.

### Plots/visualisations
**Essential (for non-experts):**
- Smoothed frequency response (1/3 octave or psychoacoustic smoothing) with target overlay.
- Before/after comparison FR.
- Simple "score" or traffic-light summary (e.g., "Bass: corrected to within ±2 dB of target").

**For experts and LLM:**
- Unsmoothed and 1/12-octave FR.
- Phase response.
- Group delay.
- Impulse response (time domain).
- Step response (low-frequency time alignment).
- Energy-Time Curve (ETC) — for identifying reflections.
- Waterfall / spectrogram (modal decay).
- Distortion (THD vs frequency from the sweep).

### Safety limits for FIR generation
- **Max gain boost**: +6 dB anywhere; +3 dB above 1 kHz. Boosting beyond is rarely beneficial and risks driver/amp distortion.
- **Min smoothing**: 1/6 octave on the target above 300 Hz; 1/24 octave or none below.
- **FDW default**: 15/15 cycles below 300 Hz; transition to 5/5 cycles by 1 kHz; 3/3 cycles above 4 kHz.
- **Tap-count cap**: 16 384 taps at 48 kHz (≈340 ms). Pi 5 handles this trivially; rarely needs more.
- **Pre-ringing audit**: compute the generated FIR's pre-impulse energy (sum of |h[n]|² for n < peak) and require it stay below a fraction (e.g., −20 dB) of post-impulse energy when linear-phase mode is selected; if not, fall back to minimum-phase or warn. (Aligns with the bodziosoftware finding that linear-phase FIR crossover order should stay under ~600 at 1–3 kHz to avoid audible pre-ringing.)
- **Boost-cap heuristic**: identify and refuse "null-filling" corrections — if the algorithm requests >+6 dB at a frequency where the unsmoothed measurement shows a >10 dB dip *and* it differs across spatial samples, treat as a null and refuse.
- **Output normalisation**: always require a built-in headroom margin (e.g., −6 dB pre-filter gain) so the FIR never causes inter-sample clipping.

### What stays deterministic vs. what an LLM advises
**Deterministic (code, audited, repeatable):**
- All filter math (sweep deconvolution, FDW, inversion, FFT convolution).
- All safety checks (gain caps, pre-ringing audit, null detection).
- Target curve application.
- CamillaDSP YAML emission.
- Session bundle write/read.

**LLM-advisory:**
- Translating user language ("muddy", "thin", "boomy") into candidate technical actions, asking clarifying questions per the table in §4.
- Explaining measurement plots to non-experts.
- Recommending whether to step up the implementation ladder (e.g., "your bass measurement shows a strong 80 Hz peak — would you like to enable FDW FIR correction?").
- Generating change summaries and audit-log narrations.
- Suggesting acoustic placement improvements when EQ can't fix the problem.

The LLM **never** writes filter coefficients directly. It selects a *strategy* (e.g., "apply a target-curve change of +1 dB shelf below 150 Hz" or "enable FDW FIR with default settings"), passes the strategy to deterministic code, which then emits the YAML.

### Staged implementation ladder
1. **Stage 0 (current)**: Bass PEQ, cuts-only, max 5 filters, 20–350 Hz, exponential sine sweep, phone mic.
2. **Stage 1 — Broader PEQ**: extend to 20 Hz–1 kHz; allow ≤+3 dB boosts below 100 Hz (only after measuring multiple positions); MMM support; vector averaging.
3. **Stage 2 — Minimum-phase FIR**: same target as Stage 1 but emitted as a single short FIR (≤4096 taps). Identical perceptual result; lays plumbing for Stage 3.
4. **Stage 3 — FDW FIR (full-range, FDW-windowed)**: psychoacoustic correction; long window below 300 Hz, short above; cuts-biased; broader than current PEQ but capped above transition.
5. **Stage 4 — Mixed-phase / excess-phase correction**: optional for users who measured carefully and want time-domain ringing reduction. Default off.
6. **Stage 5 — Preference EQ overlay**: separate filter bank, always chained *after* the room-correction bank in the CamillaDSP pipeline. Stores its state separately. The LLM's primary playground.

### Architectural separation
Two distinct YAML filter banks in the pipeline:

```
pipeline:
  - mixer: stereo_in
  - filter: room_correction_<session_id>     # objective; rewritten only by measurement flow
  - filter: preference_eq_<profile_id>       # subjective; LLM/user-driven; instant-apply
  - filter: limiter_safety                   # always-on output protection
  - mixer: stereo_out
```

The state file records active session_id and profile_id; switching profiles never touches the room-correction bank. This is the single most important architectural decision for keeping objective and subjective concerns disentangled — and for making the LLM a tool rather than a liability.

---

## Consensus Facts

1. Below the Schroeder/transition frequency (200–300 Hz in domestic rooms per Toole), EQ correction works and is needed; modal peaks should be cut, narrow nulls should not be boosted.
2. Above transition, the on-axis (anechoic) response of the speaker matters most, and the in-room steady-state response should not be force-flattened.
3. Preferred in-room response slopes downward with frequency; the slope depends on speaker directivity and room reflectivity.
4. Flat in-room response sounds too bright.
5. Multi-position averaging beats single-point measurement for room correction.
6. Multiple subwoofers reduce seat-to-seat variance more than any EQ can (Welti, Geddes).
7. FIR is necessary for arbitrary phase/time correction; IIR PEQ is sufficient for the modal region.
8. FDW is the standard technique to combine FIR correction with psychoacoustic plausibility.
9. Pre-ringing is real but avoidable with smooth targets, FDW, and gain caps.
10. ~64% of listeners prefer the Harman headphone target; ~15% want more bass; ~21% want less (Olive 2018).
11. The B&K listening curve (1974) and the modern Harman in-room curve are strikingly similar.
12. Linear-phase FIR pre-ringing is generally inaudible when used to correct natural minimum-phase rolloffs; can be audible with steep / high-Q corrections.

## Open Debates / Uncertainty

1. **Optimal downward slope number**: −0.5 vs −0.8 vs −1.0 dB/octave is unsettled and listener/speaker/room-dependent. Toole does not endorse a single number.
2. **Audibility of mixed-phase vs minimum-phase correction**: Dirac's own claims of audibly better imaging are not corroborated by published blind tests; the math is sound but the *perceptual delta* in untreated domestic rooms is small and contested.
3. **Phone microphone calibration**: there is no agreed methodology for self-calibrating an arbitrary phone mic without a reference. Smartphone mics vary substantially below 100 Hz and above 8 kHz. JTS should treat measurements as approximate and lean on relative (before/after) rather than absolute targets.
4. **How much room correction can/should "fix" above 500 Hz** when the speaker is good: the Olive 2009 product evaluation showed minimal-HF-correction products won; many commercial systems still over-correct.
5. **Best target for smart speakers specifically**: smart speakers don't behave like 2-channel hi-fi (close listening distance, omnidirectional radiation, often non-symmetric placement). The Harman in-room target is derived from hi-fi conditions; its applicability to a single-box near-field smart speaker is unproven.
6. **LLM's role in tuning**: novel — no published evaluation exists of LLM-guided audio tuning. Treat the LLM-advisory layer as research, not as a feature claim.

## Recommendations for JTS — Summary

- **Keep raw sweep recordings in the session bundle.** This is the highest-leverage forward-compatibility decision.
- **Adopt MMM as a first-class measurement mode** in addition to single-point sweeps, especially for the FIR ladder stages.
- **Implement target curve as data, not code.** Ship Harman in-room, B&K, flat, "Toole minimum-tilt", and a user-custom option.
- **Build the FIR pipeline on rePhase-compatible WAV exports** so that experts can edit in rePhase and re-import.
- **Adopt the architectural separation: room_correction filter bank vs. preference_eq filter bank** in CamillaDSP YAML.
- **Cap aggressive corrections.** Pre-ringing audit, boost caps, null detection.
- **Start mixed-phase as opt-in only.** The marginal benefit vs. measurement-error sensitivity is high; in noisy phone-mic conditions it can hurt more than help.
- **Constrain the LLM to strategies and explanations**, never to coefficients.
- **Document the limits honestly.** A smart speaker in a domestic room cannot solve SBIR, modal nulls, or non-symmetric room geometry; an LLM saying it can is a liability.

### Benchmarks / triggers for staging decisions
- Promote Stage 0 → 1 when MMM measurement is reliable across ≥10 phone models.
- Promote Stage 1 → 2 when phone-mic calibration uncertainty is shown to be <±2 dB across 100 Hz–8 kHz on representative devices.
- Promote Stage 2 → 3 when 90th-percentile correction residual ≤ ±3 dB across a tested room corpus.
- Hold Stage 4 (mixed-phase) until measurement-error sensitivity testing is published; do not ship by default.

## Glossary

- **FIR (Finite Impulse Response)**: Non-recursive filter; output is a weighted sum of N most recent input samples. Always stable. Can be made linear-phase.
- **IIR (Infinite Impulse Response)**: Recursive filter (biquad form in CamillaDSP). Efficient. Minimum-phase by construction in standard 2nd-order sections.
- **Minimum phase**: System where, for a given magnitude response, the phase response has the minimum possible energy delay. All zeros and poles inside the unit circle.
- **Linear phase**: Constant group delay; symmetric impulse response; introduces a uniform latency.
- **Mixed phase**: Decomposes a system into a minimum-phase part and an excess-phase (non-causal/all-pass) part; the only correct way to invert a non-minimum-phase room.
- **Excess phase**: The non-minimum-phase component of a system's transfer function. In rooms, this is mostly the time-of-arrival differences of reflections.
- **Group delay**: Derivative of phase with respect to frequency; not a literal time delay at a frequency but the envelope-delay of a narrowband signal around that frequency (per Dirac white paper).
- **FDW (Frequency-Dependent Windowing)**: Time-domain window applied to an impulse response whose length, expressed in cycles, is constant across frequency — so it's long in ms at low frequencies and short at high frequencies.
- **Schroeder/transition frequency**: f_s = 2000·√(RT60/V); below it the room is modal/wave-acoustic; above it it's statistical/reverberant. ~200–300 Hz for domestic rooms (Toole).
- **SBIR (Speaker-Boundary Interference Response)**: Comb pattern caused by interference between direct sound and reflection from a nearby boundary.
- **Modal region**: Frequency range below transition where individual room modes dominate.
- **Direct sound**: First-arrival wavefront from speaker to listener; the basis of timbre perception.
- **ETC (Energy-Time Curve)**: Log-amplitude vs time plot of an impulse response, used to identify reflections.
- **Waterfall / CSD**: Cumulative spectral decay plot showing decay of each frequency over time; visualises modal ringing.
- **MMM (Moving Microphone Method)**: Spatial-averaging measurement by moving the mic during pink-noise RTA averaging.
- **MLP (Main Listening Position)**: The primary seat.
- **PEQ**: Parametric equaliser (typically IIR biquad).
- **Convolution**: Mathematical operation by which FIR filters process audio (overlap-add or partitioned FFT-based in CamillaDSP).
- **Tap**: One coefficient in an FIR filter; tap count determines impulse-response length.
- **Sine sweep (ESS)**: Exponential/logarithmic sine sweep used as measurement stimulus; allows separation of linear response from harmonic distortion via deconvolution.
- **Target curve**: Desired in-room steady-state response shape, e.g., Harman, B&K, flat.

---

## Proposed JTS Markdown Corpus Outline

Each file should be self-contained, source-cited, and aimed at the next JTS agent reading it.

1. `01_dsp_fundamentals_iir_vs_fir.md` — IIR/FIR comparison, biquad math, when each is used.
2. `02_minimum_linear_mixed_phase.md` — Phase types; audibility; CamillaDSP implications.
3. `03_pre_ringing_and_psychoacoustics.md` — Pre-ringing audibility thresholds; bodziosoftware paper; safe limits.
4. `04_frequency_dependent_windowing.md` — FDW theory (Acourate, DRC-FIR), recommended cycle counts, transition rules.
5. `05_camilladsp_filter_chain.md` — YAML pipeline, `Conv`/`Biquad`/`BiquadCombo`, segmented FFT convolution, chunksize/latency math.
6. `06_pi5_dsp_budget.md` — Tap counts, CPU benchmarks (HEnquist's Pi 4 number + Raspberry Pi Foundation's Pi 5 benchmark blog), chunksize/latency/buffer relationships.
7. `07_room_acoustics_basics.md` — Modes, SBIR, transition frequency, Toole's three regions of room behaviour.
8. `08_measurement_methods.md` — Sweep capture, MMM, multi-position averaging, mic calibration, smartphone mic limitations.
9. `09_target_curves.md` — Harman, B&K, downward-slope theory, directivity dependence.
10. `10_olive_welti_preference_research.md` — Cluster analysis, demographic findings, preference variance; Sonarworks 73% flat-target user data.
11. `11_modal_region_correction.md` — PEQ cuts vs FIR; null-filling refusal; multi-position averaging trade-offs.
12. `12_full_range_correction_principles.md` — When to correct what; "Olive 2009 product shootout" findings.
13. `13_subjective_language_mapping.md` — The mapping table; clarifying questions; reference literature.
14. `14_prior_art_open_source.md` — REW, rePhase, DRC-FIR, Acourate, Audiolense, Dirac, GLM, MA 1, Sonarworks, Audyssey, HouseCurve.
15. `15_session_bundle_schema.md` — File layout, manifest, audit log, forward-compat guarantees.
16. `16_visualisations_for_users_and_llms.md` — Which plots, for whom, with what defaults.
17. `17_safety_rails.md` — Gain caps, pre-ringing audit, smoothing rules, null detection.
18. `18_implementation_ladder.md` — Stages 0–5; criteria for promotion; rollback strategy.
19. `19_objective_vs_subjective_architecture.md` — Filter bank separation; preference EQ as overlay; LLM constraints.
20. `20_llm_advisor_design.md` — What the LLM may do, must not do, must always log; question templates; uncertainty handling.
21. `21_known_unknowns_and_debates.md` — Slope, mixed-phase audibility, smart-speaker target specifics, LLM-tuning novelty.
22. `22_references.md` — Master bibliography (Toole, Olive, Welti, Geddes, Schroeder, Dirac white paper, Sbragion, HEnquist, Mulcahy).

---

## Caveats

- **Primary research budget was limited** to ~18 web searches plus one focused subagent and one enrichment pass. Some narrow claims (e.g., specific Welti spatial-variance reduction numbers in dB², detailed Geddes vs Welti placement comparisons) are reported from secondary descriptions of the original AES papers; full-text retrieval of these papers is recommended before quoting in JTS user-facing docs.
- **No first-party Raspberry Pi 5 CamillaDSP benchmark** is published by Henrik Enquist as of this writing. The Pi 5 capability claim is derived from (a) the published Pi 4 benchmark in CamillaDSP's README and (b) the Raspberry Pi Foundation's own Pi 5 benchmark blog (Oct 2023). JTS should publish its own Pi 5 benchmarks once Stage 3 is operational.
- **Olive's 64/15/21 preference clusters are from headphone research.** They are widely (and reasonably) applied to loudspeaker design but the loudspeaker-specific cluster percentages may differ slightly. Use as a guide, not as a population statistic for in-room listening.
- **Smartphone-microphone calibration remains an open problem.** All recommendations involving phone-mic measurements should be treated as relative (before/after) rather than absolute. JTS should not promise "calibrated" results from phone mics.
- **Vendor product claims (Dirac mixed-phase imaging benefits, Audyssey clustering superiority, Sonarworks "73% prefer flat") have been cited but should not be repeated to users as established science.** They are first-party usage/marketing data; independent blind-test corroboration is sparse.
- **The LLM-guided tuning layer is genuinely novel.** No published evaluation exists. Treat all §6 LLM recommendations as engineering hypotheses to be validated, not as best practices.