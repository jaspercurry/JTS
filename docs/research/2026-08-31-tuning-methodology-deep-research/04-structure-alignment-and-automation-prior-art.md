# Deep research 4 — structure, alignment, and automation prior art

> Owner-run deep research, received 2026-08-31, answering Wave 6 ticket
> 6.9's fourth assignment (see `docs/tuning-master-plan.md`). Banked
> verbatim below the rule; synthesis and any re-adjudication ADRs live
> outside this file. Frozen: no further edits.

---

Loudspeaker Tuning Toolbox: A Sourced Research Report for an LLM-Agent Design

TL;DR

* Fix structure before response. Documented pro-audio and studio-monitor practice (Bob McCarthy's Sound Systems: Design and Optimization; Rational Acoustics/Smaart curriculum; VituixCAD; the Audiofrog tuning guide) is unanimous: set polarity → inter-driver delay → crossover filters → then magnitude EQ. The user's regression is the textbook trap — two EQ campaigns fitted at 0 µs "EQ'd around" the misalignment, so the stale EQ must be discarded and re-derived after the ~100 µs delay is committed, not adjusted.
* The agent should identify topology, not assume it. Sealed-vs-ported is reliably read from an impedance sweep (one peak = sealed; two peaks with the minimum-between = ported Fb), corroborated by the woofer nearfield null at Fb (Keele, JAES 1974). By contrast, most commercial auto-EQ systems (Dirac, Genelec GLM, Trinnov, REW, MSO) ignore multi-way topology and correct only the summed in-room response, refusing to boost nulls and warning against single-point EQ above the transition/Schroeder frequency.
* Steal the safety scaffolding from QC and control theory. Klippel end-of-line QC, adaptive-optics/self-tuning control, and industrial self-tuning controllers all embody apply → measure → compare-to-limits → auto-repeat/revert. Encode: staged commit, a scalar regression metric (reverse-null depth, crossover-band summation error), monotonic-improvement gating, parameter guardrails (cap boost, prefer cut), and A/B re-measurement before any commit.

Key Findings

1. The ~60° phase criterion is practitioner convention, not a physics constant. It was popularized by Merlijn van Veen and rests on the vector-sum identity `summed level = 20·log10(2·cos(Δφ/2))`. The mathematically "clean" +5 dB point is ~55° (van Veen's "555" mnemonic); 60° is a rounded, easily-visualized tolerance. The underlying dB-vs-phase table is documented/established practice from McCarthy's Sound Systems: Design and Optimization, whose own documented additive boundary is 120°.
2. Reverse-null (polarity-inverted null-depth) is a documented, endorsed alignment-verification method — used by miniDSP app notes and PURIFI's design tech note, and (for sub/panel alignment) recommended by Linkwitz. It is extremely sensitive near the crossover but is a fine-verification tool, not an absolute-time reference.
3. Commercial active monitors treat inter-driver delay as a per-model design constant, verified against production limits, not a per-unit calibrated value. DSP monitors (Neumann KH DSP line, Genelec SAM/"The Ones") additionally linearize crossover phase in firmware. Genelec AutoCal and Neumann MA 1 calibrate the room and sub-to-main phase — not woofer-to-tweeter timing, which is factory-fixed.
4. A λ/4 offset at crossover produces only mild lobing; the damage scales with driver spacing in wavelengths and is worst when a lobe null tilts into the vertical listening window. With no vertical turntable, the agent cannot verify lobe tilt and must treat the vertical plane as a declared blind spot.
5. Impedance-based sealed/ported detection is robust; acoustic-only detection is workable but weaker. Klippel formalizes enclosure-parameter (fb, Qb) identification; confounds include damping material, port losses, leaky boxes, passive-crossover components in the path, multiple drivers sharing a volume, and amplifier output impedance.
6. Every documented correction product refuses to "EQ a null" and warns against single-point EQ above the transition frequency — the strongest cross-cutting safety rule in the literature (Toole; Mulcahy/REW; Dirac; Genelec).

Details

1. Time-alignment practice for vertically arrayed multi-ways

1a. The phase-overlay method and the 60° criterion — provenance and confidence. The method aligns so the two drivers' unwrapped phase traces lie within a tolerance band across the crossover octave; the commonly quoted tolerance is ~60°.

* Provenance (labelled). The 60° "corridor" is a practitioner convention popularized by Merlijn van Veen ("Subwoofer Alignment: The foolproof relative/absolute method," merlijnvanveen.nl, 2019). He derives it from the summation math: "When you sum two sine waves (pure tones) of equal magnitude but with 55° of phase offset you will still gain 5 dB summation… Very easy to remember '555'." He then rounds: "Sixty degrees is awfully close to fifty-five degrees, so you could argue that any phase offset of 60° or less should suffice for 5 dB of summation or more, provided your levels are matched," operationalized as a "60-degree-wide corridor" the traces must live within ("How loud can I go? Until the corridor of 60° stops!"). (Confidence: high on provenance; the number is a teaching heuristic, not a derived constant.)
* The theoretical justification — the summation table (documented/established). For two equal-level correlated sources, `summed level = 20·log10(2·cos(Δφ/2))`. Verified values: 0°→+6.0 dB; 50°→+5.2 dB; 55°→+5.0 dB; 60°→+4.8 dB; 90°→+3.0 dB; 120°→0 dB; 150°→−6 dB; 180°→full cancellation. The authoritative published presentation of this as "summation zones" is Bob McCarthy, Sound Systems: Design and Optimization (Focal Press; 1st ed. 2007, 3rd ed. 2016, ISBN 9780415731010, "Summation" chapter), whose documented boundary is the 120° "coupling zone" limit (phase offset must be <120° for the combination to remain additive). McCarthy's ProSoundWeb "Audio Summation Part 3" restates the milestones (0°→+6, 90°→+3, 120°→0, 150°→−6) and credits van Veen for the "+5 dB memory helper." Net: the table is established practice; the 60° tolerance is convention layered on top (60° ≈ half the 120° additive window). (Confidence: high.)
* Effect of steeper slopes. A steeper crossover (LR4 vs LR2) narrows the band over which both drivers contribute, so phase mismatch matters over a narrower region — but within that region summation is more sensitive to delay error, because steeper phase slopes convert a given time offset into more degrees of rotation at crossover. This "tighter region, more sensitive inside it" reading is consistent with the McCarthy framework and is the DIY/VituixCAD-community consensus. (Confidence: medium-high; widely held and theory-consistent, but presented mostly in training/forum form.)

1b. Null-depth / reverse-null methods. Invert one driver's polarity; if the two are in-phase and level-matched, they cancel at crossover, producing a deep notch, and the delay that maximizes notch depth indicates alignment.

* Documented endorsement. miniDSP's "Driver time alignment" app note: "invert one driver and run a measurement sweep… they will cancel at the crossover frequency and create a null." PURIFI's tech note: "By reversing the polarity of one driver we can verify that we get a null on axis (the reverse null)." For sub/panel integration, an ARTA-based writeup reports the minimization ("cancellation") method was "specifically recommended by Linkwitz himself."
* Depth targets & pitfalls. There is no single published dB target; practitioners treat "as deep as possible" as the goal and note the null varies wildly with tiny mic movement and reflections (ARTA writeup: at the deep null "it actually varies a lot"). Encode: (i) window the impulse to exclude reflections in-room; (ii) it is a relative alignment check, establishing no absolute time-zero; (iii) level mismatch caps achievable depth; (iv) use it for fine verification after a coarse alignment. (Confidence: high on method; numeric depth targets are convention/opinion.)

1c. Impulse/step-response and excess-phase methods.

* Peak-of-impulse / cross-correlation finds bulk delay but is frequency-biased. McCarthy explicitly warns against impulse-peak for spectral crossovers: the IR peak gives "the bulk of the energy," but "linear frequency math does not have a fair and balanced perspective over frequency," so the peak is dominated by HF content and misrepresents crossover-frequency timing. (Documented expert opinion, strongly argued.)
* Minimum-phase extraction / excess phase. PURIFI gives the rigorous framing: a driver response = (minimum-phase part) × (excess-phase part), where the excess-phase part "simply is a delay corresponding to the time of flight." A minimum-phase EQ preserves latency; only an explicit delay (or all-pass, or baffle geometry) corrects inter-driver latency. Hence the "flat excess phase / matched excess group delay across the overlap" criterion is the most defensible physics-based alignment test. VituixCAD supports it directly: "Mod-delay… can be determined via Tools → Aux → Time Align," using dual-channel measurements so no minimum-phase reconstruction is needed. (Confidence: high.)

1d. Loopback vs acoustic timing references.

* REW supports an acoustic timing reference (a reference speaker plays a timing sweep before each sweep) so relative delays between drivers/measurements are preserved; documented constraint: the reference must be HF-capable ("a subwoofer cannot be used as the reference channel").
* Absolute vs relative. For inter-driver delay you only need a consistent relative reference across both driver captures — absolute time-zero is not required if the reference is identical for both. Loopback gives absolute electrical-domain zero; the acoustic reference gives a stable acoustic zero. Gotchas to encode: soundcard latency drift between captures; ASIO vs WASAPI latency differences; USB-mic latency (UMIK-class mics have no loopback, so an acoustic timing reference is mandatory for cross-measurement timing). (Confidence: high.)

1e. How commercial makers set/verify inter-driver delay.

* Design constant, not per-unit. The inter-driver acoustic offset is fixed by geometry + crossover and is a per-model design value; production QC verifies frequency/phase against limits (Klippel end-of-line pass/fail) rather than dialing a bespoke per-unit delay.
* DSP monitors linearize crossover phase in firmware. Neumann's MA 1 page states: "KH 120, KH 310, and KH 420 loudspeakers profit from phase linearization of their built-in crossover filters! The result is increased clarity, a dry bass, and a time-correct… depiction of reverbs." The KH 750 DSP spec quantifies it: "FIR Phase Correction: For Connected Analog Loudspeakers; Linear Phase (170 Hz … 16 kHz; +/- 45°)." KH 120 II / KH 150 have "phase linear crossovers" via internal DSP. Genelec "The Ones" have a "wide phase linearity setting"; per audioXpress, "GLM 4.1 accurately time-aligns all the monitors in a room, across types, taking The Ones' wide phase linearity setting into account," via AutoCal 2.
* AutoCal / MA 1 calibrate room + sub phase, not woofer-tweeter timing. Genelec's Room Response Compensation page: "AutoCal also aligns relative levels, time-of-flight, as well as adjusts correct crossover phase (called AutoPhase) for all subwoofers on the network" and applies compensation "in the low and low-mid frequencies." Genelec Support adds that "We compensate the room response using minimum phase filters," and that the monitors are "time-domain optimized out of the factory… so there's no need to equalize the time" for the monitor itself — GLM only phase-aligns the subwoofer. Neumann MA 1 (developed with Fraunhofer IIS) corrects amplitude + phase at the listening position. (Confidence: high.)

1f. How severe is λ/4 lobing?

* Physics. Two vertically separated sources sum with a path-length difference that grows off-axis; with a symmetric even-order (LR) crossover the main lobe points on-axis, and asymmetries — including uncompensated inter-driver delay — tilt it. PURIFI: uncorrected latency "will cause the lobes to tilt and no longer point straight."
* Quantified rules of thumb (VituixCAD community; Kimmo Saunisto and others): center-to-center spacing ~0.5–0.7 λ at crossover is the worst case (deep power/off-axis dip); ~1.0–1.2 λ is the conventional upper bound; ≤ λ/4 is the "holy grail" giving near-hemispherical vertical coverage. At λ/4 with an LR alignment, the theoretical first null is pushed toward ±90° vertical (≈ −3 dB near ±90°) — essentially no null in any realistic listening window. As spacing rises toward 1 λ, nulls move into the ±20–40° vertical region. (Confidence: medium-high on the λ-spacing rules; exact null angles depend on driver directivity and require simulation.)
* Listening window. DIY authorities note "with many popular designs the listening window can be as small as 20 degrees vertically." For a seated listener at 2–3 m, a ±5–10° vertical window is typical; the risk is a lobe null falling inside it. With only a single-axis (horizontal) turntable you cannot measure this — a genuine blind spot the agent must flag rather than silently accept.
* Toole / Vanderkooy-Lipshitz framing (established). Toole's Sound Reproduction holds that on-axis + smooth off-axis (including vertical) governs perceived timbre; crossover lobing error is a real coloration mechanism, minimized by low crossover + close spacing rather than corrected by EQ. (Confidence: high on the qualitative principle.)

2. Order of operations

2a. "Structure before response" (documented/established).

* Rational Acoustics / Smaart curriculum (via Sound Design Live, citing McCarthy): the verification sequence sets polarity, checks that phase traces are "within 60° through the crossover region," achieves summation, and only then applies "any necessary combined EQ."
* Audiofrog tuning guide (explicit): "Polarity, delay, crossovers, level and EQ, confirmation and additional level adjustments. That's the order."
* VituixCAD workflow (Kimmo Saunisto docs): measure drivers (dual-channel, preserving relative timing) → set inter-driver delay via Time Align → design crossover → EQ each driver's contribution inside the crossover model, not bolted onto a misaligned sum.
* McCarthy frames EQ as the last corrective layer: "a level-band-aid is not gonna remedy a time problem." (Confidence: high — the single most consistently documented rule across pro-audio, studio, and DIY sources.)

2b. The documented trap the user hit — "EQ'ing around" a misaligned sum. Mechanism: response EQ fitted on a summed response that already contains a phase-cancellation dip (from the uncorrected offset) encodes a boost/notch that compensates for the misalignment. Apply the correct delay later, and the summation dip moves/disappears while the compensating EQ remains — so the corrected sum is now wrong. Documented articulations:

* Van Veen: an engineer landing in a crossover-region null "is likely going to try to fix [it] with EQ. However, a level-band-aid is not gonna remedy a time problem. It will not improve the situation… and make things worse for all other audience members."
* ProAudioFiles/Smaart: an RTA "does not understand phase, and therefore will interpret a polarity mismatch as simply a drop in level, which you will then try to fix with an increase in level" — the same category error.
* Toole/REW principle (below): a dip caused by phase interaction is not minimum-phase there and cannot be EQ'd; boosting it wastes headroom and mis-shapes nearby response. Rule for the agent: any EQ fitted while delay/polarity were wrong is invalid — discard and re-derive after structure is committed. (Confidence: high on the mechanism, even if not always under one catchphrase.)

2c. Chained delay bookkeeping (3-way+).

* Reference driver = the most-delayed / farthest-arriving, usually the woofer, set to zero delay; the mid and tweeter get positive delays to line up to it. miniDSP's 3-way app note does exactly this: set woofer delay to zero, midrange delay to the woofer↔mid acoustic delay, tweeter delay to its earlier-computed value.
* Pitfall: changing a crossover frequency changes the phase slopes and therefore the effective offset at the new crossover — so delays must be re-derived after any crossover-point change, not carried over. MSO documents that room modes make "optimized delays not match predicted delays," so it re-optimizes rather than trusting bookkeeping.
* Sign convention to encode: positive delay = signal held back; delay the earlier-arriving driver; keep one reference at 0 and store all others relative to it, with an explicit note of which physical driver is the reference. (Confidence: high.)

3. How existing auto-EQ systems handle speaker topology

General finding: most room-correction/auto-EQ systems do NOT branch on multi-way topology (horn vs dome, ported vs sealed, way-count). They operate on the summed in-room response and branch mainly on sub vs main and on frequency region (below vs above transition).

* REW (John Mulcahy). Not an auto-topology system. Documented EQ guidance: limit filters to LF (<~200 Hz) unless deliberately using EQ "as a fancy tone control"; "EQ doesn't often help with dips… applying EQ to a dip in one measurement position may create an unwanted boost a short distance away." REW's minimum-phase analysis lets the user see where the response is minimum-phase (EQ-able) vs not. It asks nothing about topology and imposes individual + overall max-boost guards. (Documented.)
* Dirac Live (+ Bass Control / ART). Uses mixed-phase (IIR+FIR) correction to address the impulse response and reduce pre-ringing. Dirac's paper "On Room Correction and Equalization of Sound Systems" notes "For a subwoofer channel, however, minimum-phase inversion will typically be sufficient," with mixed-phase pre-ringing kept "60 dB below the peak." CEO Mathias Johansson: "A lot of room acoustic behavior is non-minimum-phase and only a proper mixed-phase correction using multiple measurements can maximize the tightness of the bass." Dirac corrects magnitude + impulse across a listening window, asks nothing about driver topology, and does not claim to fix non-minimum-phase room nulls by boosting. (Documented.)
* Genelec GLM / AutoCal (AutoCal 2). Branches on monitor vs subwoofer, applies AutoPhase only to subs, refuses to time-correct the (factory-optimized) monitor, and applies room compensation using minimum-phase filters concentrated in the LF/low-mid. (Documented.)
* Trinnov Optimizer. Separates loudspeaker (direct + early) from room (energy) and corrects both magnitude and phase with FIR; "all the subtlety of the Optimizer resides in knowing which defects can be corrected without creating additional problems." Adds remapping (AES paper 6375). Does not ask for driver topology; works from a 4-capsule 3D mic. (Documented.)
* MSO (Andy C.). Purpose-built for multi-sub integration and seat-to-seat variance, not full-range multi-way EQ. Requires user-specified DSP filter structure and timing-referenced measurements; documents that it will not chase SPL blindly (it added an SPL-penalty metric so the optimizer can't knock a "problematic sub" down just to win flatness). (Documented — an excellent source of safety-metric design.)
* VituixCAD (Kimmo Saunisto). Not an auto-EQ — a simulation/design tool. It does branch on topology (enclosure type, port/passive radiator, driver coordinates → vertical lobing) and requires the user to declare all of it. It is the closest model for a topology-aware tool. (Documented.)
* Acourate (Uli Brueggemann) / Audiolense (Bernt Rønningsbakk). FIR-based; allow linear/mixed-phase targets and time-domain correction; the user drives topology decisions. (Confidence: medium — widely used with developer docs; no specific developer statement fetched this pass.)
* Audyssey / ARC Genesis / Anthem / Lyngdorf RoomPerfect / Sonarworks. Consumer/prosumer, magnitude-focused (Audyssey/YPAO/MCACC are simpler parametric per Archimago); RoomPerfect measures many positions to separate room from speaker; none branch on driver topology. (Confidence: medium.)

What they refuse to automate & why: boosting deep nulls (position-dependent, non-minimum-phase, wastes headroom); aggressive EQ above the transition/Schroeder frequency from a single position (Toole/Geddes: it corrupts the direct sound); and phase "correction" that would destabilize crossovers. These refusals are the guardrails to copy.

4. Automatic topology identification from measurements

4a. Impedance-sweep detection (robust, documented).

* Sealed = one impedance peak (Fc). Ported = two peaks; the minimum between them is the box tuning Fb (Helmholtz). Standard and repeatedly documented (diyAudio; Archimago; D'Appolito's Testing Loudspeakers is the canonical practitioner reference). Relative peak heights indicate Fb vs Fs (left peak higher → Fb<Fs). Benson's cross-check: `Fb=(Fl²+Fh²−Fc²)^½` after plugging the port.
* Passive radiator looks ported-like (two peaks) plus a sharp notch at the PR resonance.
* Confounds to encode: heavy damping/port losses flatten and merge peaks; leaky boxes raise the minimum; a passive crossover adds its own impedance features (e.g., a tweeter peak); high-Z/current-drive amplifier output impedance distorts the curve; multiple drivers sharing a volume must be measured as connected. (Confidence: high.)
* Rigs: Dayton DATS V3, Woofer Tester, CLIO, and Klippel all measure impedance; DATS-class is adequate for Fb extraction.

4b. Klippel literature (documented). Klippel's R&D system identifies full linear (Thiele-Small) + enclosure parameters (fb, Qb) from electrical input current alone ("neither microphones nor mechanical sensors are required"), and the driver "may be operated in free air as well as in sealed or vented enclosures, giving additional enclosure parameters (fb, Qb)." The Near-Field Scanner (NFS) uses spherical-harmonic near-field holography to derive far-field/directivity and separate direct from reflected sound; a Klippel training explicitly compares vented/sealed/free-air via near-field scanning. Klippel's large-signal work reports a fit-error metric — "the relative error in the system identification becomes very small (about 1 %) showing a good fitting" — a directly stealable confidence gate for parameter extraction. (Confidence: high.)

4c. Acoustic-only detection (workable, weaker).

* Port presence: the woofer nearfield response shows a minimum (null) at Fb where cone motion is minimized by the port's acoustic load; the port nearfield shows a peak at Fb. The canonical method is D.B. Keele Jr., "Low-Frequency Loudspeaker Assessment by Nearfield Sound-Pressure Measurement," JAES Vol. 22, No. 3, pp. 154–162, April 1974, in which scaled nearfield woofer + port outputs are summed to reconstruct the LF response; as Stereophile summarizes the diagnostic, "the woofer output drops to a minimum at the port tuning… frequency… almost all the speaker's output is coming from the port." A woofer nearfield null is thus a strong port indicator.
* Rolloff slope & group delay: sealed ≈ 12 dB/oct (2nd-order) with modest group delay; ported ≈ 24 dB/oct (4th-order) with a group-delay bump near Fb — distinguishing topology if clean LF (nearfield/anechoic-equivalent) is available.
* Horn/waveguide loading: impedance ripple (mouth reflections) plus strong directivity narrowing and a loading peak.
* Reliability without impedance: medium. Nearfield null at Fb is fairly diagnostic; slope/group-delay inference is confounded by room gain, mic placement, and any EQ already in the chain. Use impedance as primary, acoustic as corroboration. (Confidence: medium-high.)

5. Documented failure modes of "measure, model, correct"

Each of the following is a documented case where naive auto-EQ misleads or does harm — encode each as a hard guardrail:

* Deep room nulls (below Schroeder) — cannot be EQ'd. Established (Toole; Mulcahy/REW; REW community): "If you try to boost a null, the room will eat up… any amount of power you pump into the room… and makes frequencies in the vicinity worse." REW ties EQ-ability to deviation from minimum phase: EQ works where the response is minimum-phase, fails where it isn't.
* EQ above the transition/Schroeder frequency from one mic position. Toole and Geddes argue against corrective EQ in the "stochastic region" because a single point doesn't represent what's heard and EQ corrupts the direct sound; REW's default guidance limits filters to LF. (Documented; note contested for FIR/mixed-phase — see Caveats.)
* Dipoles / open baffles. The ~6 dB/oct dipole rolloff and baffle peak are largely minimum-phase and EQ-able (Linkwitz EQ'd his dipoles by design), but the deep dipole null and room interaction are not; naive full-range room EQ misreads the dipole rolloff as an error. Treat dipole/cardioid as a declared topology. (Confidence: medium-high.)
* Line arrays / column speakers. Measurement distance (near-field vs far-field) matters, and comb filtering between elements is position-dependent — EQ at one point makes others worse.
* Subwoofer integration below Schroeder. Single-point vs spatial-average diverge; you can't EQ a null; use multiple subs + spatial optimization (MSO) instead of boost.
* Port / pipe (organ-pipe) resonances. A port's internal pipe resonance radiates from the port, not the cone; an electrical notch attenuates the driver drive at that frequency but does not address port radiation the way acoustic treatment (port geometry, stuffing) does — so electrical vs acoustic fixes are not equivalent. (Confidence: medium; physically sound, documented in DIY literature.)
* Cabinet diffraction: min-phase vs non-min-phase. Baffle-step and broad diffraction dips are minimum-phase (PURIFI: baffle step "preserves the minimum-phase property") and EQ-able; sharp edge-diffraction ripple and off-axis interference are direction-dependent and not cleanly EQ-able. (Confidence: high on baffle step, medium on the boundary.)
* Horn "honk." A midrange resonance/loading artifact (minimum-phase, EQ-able) must be distinguished from a reflection/higher-order-mode artifact (not cleanly EQ-able); Geddes' HOM work is the reference.
* Excessive boost → excursion/thermal limits. Any boost raises excursion (∝ boost) and power/thermal load; cap boost (REW exposes individual + overall max-boost) and prefer cut over boost. (Documented.)

6. Prior art on autonomous / agent-driven measure-and-correct loops

* Klippel end-of-line QC (audio, closest analog). Fully automated: stimulus → measure → compare to pass/fail limits → verdict; auto-repeat + merging of valid data when ambient noise corrupts a measurement; ambient-noise/temperature/humidity sensors reject invalid measurements; golden-unit reference + statistical limit-setting; and the EQA (Equalizer Adjustment) module actually assists an operator to adjust factory EQ to a target, iterating "until the target response is achieved within specified tolerance" — a direct precedent for a bounded, target-driven tuning loop with a tolerance gate.
* Adaptive optics closed loops. RL and pseudo-open-loop controllers minimize a residual wavefront error each cycle; the pattern is a scalar residual optimized under stability constraints, with system identification used to bound robustness.
* Process-control self-tuning controllers (Bristol; Kraus patents). Adaptation triggers only when error exceeds a noise band; closed-loop response features are identified; new tuning is generated to hit a target error shape — i.e., don't act on noise; act on significant deviations, toward an explicit target.
* Self-healing adaptive controllers build a "stable closed-loop model reference library" and move the system from an off-nominal state to a "safe reachable normal condition," minimizing loop error — a checkpoint/rollback-to-known-good pattern.
* Genelec GLM / Neumann MA 1 are the consumer-facing automated audio calibration flows: guided measure → compute → store-in-DSP, with correction bounded to regions the developer trusts (LF/room; sub phase).

Safety patterns to steal (encode explicitly):

1. Apply → verify → rollback. Never commit a change without a re-measurement confirming improvement on a scalar metric; auto-revert on regression.
2. Monotonic-improvement gating. Commit only if the target metric (crossover-band summation error, reverse-null depth, listening-window flatness) improves; break ties toward less processing/delay (Smaart practice).
3. Parameter guardrails. Hard caps on boost (prefer cut), delay range, and filter Q; refuse to EQ where coherence is low or the point is non-minimum-phase.
4. Validity gate before commit. Reject measurements with low coherence/noise (Smaart: "Never EQ a frequency where the coherence trace is low"; Klippel auto-repeat).
5. Checkpoint / known-good baseline. Keep the pre-change state ("name the first config baseline and don't change it" — MSO practice) for instant rollback.
6. Staged commit with human-in-the-loop gates at structural transitions (after polarity/delay; after crossover; after EQ).
7. A/B re-measurement as the definition of "better," not model prediction alone (MSO documents predicted ≠ measured due to room modes).

Recommendations (staged, with thresholds)

Stage 0 — Identify topology and establish confidence.

* Run an impedance sweep. Decision rule: 1 peak → sealed; 2 peaks → ported with Fb = frequency of the minimum between peaks; extra sharp notch → passive radiator. Corroborate with a woofer nearfield sweep (null at Fb ⇒ port confirmed — Keele, JAES 1974). Store a confidence flag; if impedance and acoustic disagree, mark topology "uncertain" and require human confirmation.
* Borrow Klippel's fit-error gate concept: only trust an extracted Fb if the two methods agree within a few percent.

Stage 1 — Structure (where the user's fix belongs).

* Discard both prior EQ campaigns entirely — they are invalid because they were fit on a misaligned sum. Do not try to "adjust" them.
* Commit the measured ~100 µs offset (tweeter forward → delay the tweeter). Verify with the reverse-null: invert one driver, sweep, maximize null depth at 2.5 kHz on the design axis, windowing the impulse to exclude reflections. Target the deepest, most symmetric null achievable; a practical "good" heuristic is a notch ≥ ~15–20 dB below the summed level — label this as convention, not a published standard.
* Cross-check with the phase-overlay/60° corridor: the two drivers' phase traces should lie within ~60° across the crossover octave (established table; 60° is a convention guaranteeing ≥ +4.8 dB summation).

Stage 2 — Crossover.

* With delay committed, confirm the LR4 targets still hold. If you change the crossover frequency, re-derive the delay — do not carry it over.

Stage 3 — Response EQ (last).

* Only now fit magnitude EQ, on the correctly-summed response. Prefer cut over boost; cap boost (mirror REW's individual/overall max-boost). Do not EQ points where coherence is low or where REW's minimum-phase analysis shows large excess phase. Above the transition frequency, EQ only broad trends from a spatially-averaged measurement, not single-point detail (Toole/Mulcahy).

Stage 4 — Vertical blind spot.

* With only a horizontal turntable you cannot verify vertical lobing/lobe-tilt — flag this explicitly. Mitigations: (i) rotate the cabinet 90° on the existing turntable to capture a coarse vertical polar set; (ii) simulate expected vertical lobing in VituixCAD from measured driver spacing, crossover, and the committed delay; (iii) at minimum, log that lobe tilt from the now-corrected delay should be near-zero on the design axis — the best obtainable assurance without vertical data.

Metrics that should change the plan:

* If reverse-null depth cannot exceed ~10 dB after delay/level matching, suspect residual polarity/level/frequency-dependent-offset issues (or non-flat driver overlap) — stop and diagnose, don't paper over with EQ.
* If applying the correct delay makes the summed magnitude worse, that is expected given the stale EQ — it confirms the EQ was compensating for misalignment; proceed to discard and re-fit.
* If topology detection is "uncertain," gate to human confirmation before any LF EQ.

Caveats and confidence labeling

* Established / documented practice (high confidence): structure-before-response ordering; the dB-vs-phase summation table and its `20·log10(2·cos(Δφ/2))` basis (McCarthy); reverse-null as a verification method (miniDSP, PURIFI, Linkwitz); the impedance one-peak/two-peak sealed-vs-ported rule (D'Appolito, widely replicated) and Keele's nearfield method (JAES 1974); "can't EQ a null / minimum-phase-only EQ" (Toole, Mulcahy/REW); Klippel QC pass/fail + auto-repeat pattern; manufacturer delay-as-design-constant + DSP phase linearization (Neumann KH 750 FIR spec ±45°, Genelec AutoCal/AutoPhase).
* Practitioner convention (medium confidence, labelled): the specific 60° tolerance (van Veen; the exact +5 dB point is ~55°); reverse-null "depth target" numbers (no published standard); the λ-spacing lobing rules of thumb (0.5–0.7 λ worst, ≤ λ/4 ideal) — well-established in the VituixCAD community but simulation-dependent for exact null angles.
* Contested / individual opinion (flagged): whether corrective EQ above the transition frequency is ever beneficial (Toole/Geddes: largely no for minimum-phase; some FIR/mixed-phase advocates disagree); McCarthy's rejection of impulse-peak alignment for spectral crossovers (well-argued expert opinion, not universal); the audibility magnitude of crossover phase linearization (manufacturers assert clear benefit; controlled blind-test evidence is mixed).
* Sourcing gaps to close before production: Acourate/Audiolense specifics rest on general knowledge rather than a fetched developer statement; consumer systems (Audyssey/ARC/Anthem/RoomPerfect/Sonarworks) topology behavior is summarized from secondary sources; an exact page/edition citation for McCarthy's summation table was not obtained from the book itself (target: 3rd ed., 2016, ISBN 9780415731010, "Summation" chapter). Production-test descriptions for PMC/ATC/Dynaudio/Focal/Kii/Dutch&Dutch/Grimm were not individually located and should be treated as "not found," not "nonexistent."
