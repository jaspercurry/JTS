# Time-Selective Excitation vs. an Early Boundary Reflection: Is Pulsed/Swept Time-Gating Viable, and What Beats the Time-Bandwidth Limit at One Point?

> Owner-commissioned deep-research report #2, delivered 2026-07-25.
> Preserved verbatim (primary source); the adopted synthesis lives in
> [`flat-linearization-plan.md`](../../flat-linearization-plan.md)
> ("Adjudicated: single-point time-selection"), which wins where they
> disagree. Commissioned to evaluate the owner's pulsed-sweep /
> fast-sawtooth proposal against the measured τ≈0.31 ms boundary bounce,
> with four pre-registered predictions (graded at the end).

## TL;DR

- **Your predictions are essentially correct.** For an LTI DUT, both proposed variants (pulsed lull-excision and fast frequency-tracking sweeps) are mathematically equivalent to applying a ~0.31 ms time gate; variant B is literally Heyser's TDS, and TDS = windowing is settled (Vanderkooy 1986; Müller & Massarani 2001). No excitation design escapes the time–bandwidth limit without model assumptions or extra channels; separating arrivals 0.31 ms apart costs ~1/τ ≈ 3.2 kHz, making everything below ~3.2 kHz unreachable by pure gating/TDS.
- **A true echo-free lull does not exist for your DUT.** An LR4 @ 2 kHz acoustic crossover is an all-pass with a group-delay peak on the order of several hundred microseconds at crossover, and a compression horn adds its own delay/ringing; the DUT's own response to any excitation persists well past 0.31 ms, so the echo of the early response overlaps the direct tail. Your empirical result (0.29 ms gate kills the comb but biases level down several dB) is the expected signature.
- **The only theoretically sound single-point escape is model-based super-resolution**, and it is unproven at consumer SNR for a ~0.31 ms pair with a frequency-dependent reflection coefficient r(f). Given spatial power averaging already ships, the highest-value additions are a **zero-cost ultra-short-gate HF cross-check** (>3.2 kHz) and, as a research bet, a **cepstrum-seeded regularized two-path inversion** — pulsed lull-excision itself adds nothing over gating.

-----

## Key Findings

1. **TDS is gating; gating is TDS; pulsed excitation with time-selective analysis is gating.** All three are the same LTI operation viewed in different domains. The TDS low-pass filter is exactly equivalent to a time window on the impulse response — stated explicitly by Müller & Massarani (2001) and formalized by Vanderkooy (1986).
1. **The quantitative wall.** To separate a direct arrival from a reflection Δt = 0.31 ms later, any gating/TDS scheme forfeits frequency information below f_min ≈ 1/Δt ≈ 3.2 kHz. The TDS route reaches the identical number: with Vanderkooy's resolution = √(sweep rate), rejecting a τ-delayed echo requires (sweep rate)·τ ≳ √(sweep rate), i.e. √(sweep rate) ≳ 1/τ ≈ 3.2 kHz of resolution loss — the same bound.
1. **No lull.** LR4 + horn ring past 0.31 ms; there is no excitation waveform that makes the DUT's own response vanish before the echo arrives. Confirmed by theory and by your own 0.29 ms-gate experiment.
1. **No shipped product uses pulsed lull-excision.** Burst/tone-burst methods exist for power handling, resonance/burst-decay (CSD alternatives), and interrupted-noise reverberation-time measurement — none is a reflection-rejection lull-excision precedent.
1. **Super-resolution can beat the Fourier limit at one point — but only under a sparse/parametric model, and r(f) is its Achilles heel.** Demonstrations exist mostly in simulation with known count K and constant scalar amplitudes; no measured single-mic ~0.31 ms demonstration at 30–50 dB SNR was found, and frequency-dependent amplitude provably biases MUSIC/ESPRIT.

-----

## Details

### QUESTION 1 — First principles and the TDS story

**(a) Consensus answer with equations/numbers.**
For an LTI DUT the measured response is H(f)·(excitation). All three techniques reduce to selecting a time interval of the impulse response h(t):

- **(a) Time-gating**: multiply h(t) by a window w(t) of length T; equivalently convolve H(f) with W(f). A gate that ends before the echo at τ has length T ≤ τ, so its spectral smoothing kernel has width ≈ 1/T ≥ 1/τ. The lowest frequency with independent information is f_min ≈ 1/T.
- **(b) TDS (Heyser, JAES 1967)**: a linear sweep df/dt = S (Hz/s) is emitted; the analyzer multiplies the return by a delayed sine/cosine and low-passes. A reflection delayed by τ arrives at an instantaneous-frequency offset Δf = S·τ relative to the tracking filter. The filter/analysis bandwidth is B ≈ √S (Vanderkooy 1986: frequency resolution in Hz = √(sweep rate in Hz/s)). Rejecting the reflection needs Δf ≳ B ⇒ S·τ ≳ √S ⇒ √S ≳ 1/τ. Thus the achievable frequency resolution √S is bounded below by 1/τ — **exactly the gating limit**. Müller & Massarani (2001) state that "the impact of the TDS low-pass filter is equivalent to the application of a window to the captured IR, and any windowing reduces the spectral resolution," and that TDS "is not capable of complete reflection suppression due to the limited steepness of the low-pass filters." They also make explicit why a *linear* (not log) sweep is required — verbatim: "The controlled suppression of reflections is the motivation why TDS analyzers utilize a linear sweep (df/dt = constant). The frequency difference between incoming direct sound and reflection will thereby stay constant over the whole sweep range, keeping the attenuation of each reflection frequency-independent."
- **(c) Pulsed/interrupted excitation with time-selective analysis**: for an LTI system the response to a burst train is the superposition of windowed impulse responses; excising the "echo-free" segments and stitching is algebraically identical to windowing h(t). It carries no information a single gated sweep lacks. (Farina log-sweep + deconvolution already yields the full h(t); a burst scheme cannot add information to an LTI measurement.)

**Time–bandwidth theorem.** The Gabor/Heisenberg uncertainty bound σ_t·σ_f ≥ 1/(4π) (equality for Gaussians) is the general statement. Operationally, resolving two arrivals Δt apart from a single scalar channel, with no parametric prior, requires bandwidth-limited resolution Δf ≳ 1/Δt. For Δt = 0.31 ms: **f_min ≈ 3.2 kHz**. This matches your comb geometry: nulls spaced 1/τ ≈ 3.2 kHz apart (first null ≈ 1.6 kHz), and coherent LF lift 20·log₁₀(1+r) = 20·log₁₀(1.36) ≈ **+2.7 dB** for r = 0.36.

**Is there ANY excitation-design theorem that circumvents this?** No — **not for a single-channel LTI measurement without additional assumptions**. The only escapes require (i) a parametric/sparse model of h(t) (Question 4a), which is an added assumption, or (ii) additional spatial channels (Question 4b/4c). Within the class "design a better waveform," the answer is a flat no: any excitation you emit is deconvolved to the same h(t), and everything downstream is a linear operator on that fixed h(t).

**(b) Shipped-product vs academic evidence.**

- *Academic*: Heyser, "Acoustical Measurements by Time Delay Spectrometry" (JAES 15(4):370–382, 1967); Vanderkooy, "Another Approach to Time-Delay Spectrometry" (JAES 34(7/8):523–538, July/Aug. 1986); Müller & Massarani, "Transfer-Function Measurement with Sweeps" (JAES 49(6):443–471, 2001); Farina swept-sine (AES 108th Conv., 2000). Uncertainty principle: standard (Gabor).
- *Shipped-product*: TDS was productized (Techron/Crown TEF analyzers, GenRad); modern loudspeaker tools (CLIO, ARTA, REW, Klippel) use log-sweep + deconvolution + windowing rather than analog TDS. No shipped tool claims to beat the gating limit by waveform design.

**(c) Citations.** Heyser 1967; Vanderkooy 1986; Müller & Massarani 2001; Farina 2000; Gabor/Heisenberg uncertainty (DSP standard).

**(d) Confidence + contested points.** **Very high** that TDS = gating and that no waveform beats the limit for single-channel LTI. Historically contested point: some early TDS-vs-MLS comparisons (which Müller & Massarani rebut) assumed a fixed TDS low-pass and derived "exorbitant" sweep lengths; the resolution of that dispute is that TDS resolution equals periodic-excitation resolution when the low-pass is removed (double-excitation), so the equivalence holds.

**(e) Design consequence.** Stop treating variants A/B as candidate methods — they are re-derivations of a 0.31 ms gate and cannot reach below ~3.2 kHz; spend the effort on the sub-3.2 kHz region, which only spatial averaging or a parametric model can touch.

-----

### QUESTION 2 — The DUT-ringing wall

**(a) Consensus answer with equations/numbers.**
A loudspeaker is (near-)minimum-phase in-band; a flat, well-damped driver rings ≈ one cycle at a given frequency. But your system response is dominated by two lengtheners at/near the gate scale:

- **LR4 @ 2 kHz acoustic crossover** is an all-pass on summation; a 4th-order LR crossover's summed phase equals that of a 2nd-order all-pass, whose group delay peaks near fc. For fc = 2 kHz (period 0.5 ms), the group-delay peak is on the order of several hundred microseconds and the combined impulse response has the characteristic all-pass overshoot/ring extending on the order of ~1 ms (a few cycles of ~2 kHz). This alone exceeds 0.31 ms.
- **Compression horn**: adds propagation delay through the horn plus HF resonances/mouth reflections and its own decay, extending the tail further and unevenly across frequency.

Truncating h(t) at 0.3–0.6 ms therefore (1) discards real DUT decay energy, biasing magnitude **downward** (most at LF and near crossover where the tail is longest), and (2) convolves H(f) with a wide (≥1/T) kernel, smoothing/erasing detail below f_min ≈ 1/T. A 0.31 ms gate ⇒ no valid data below ~3.2 kHz and level bias of several dB — exactly your observed "0.29 ms gate removes the comb but biases levels down several dB." Standard practice (D'Appolito/CLIO; Struck & Temme 1994; REW/ARTA docs) codifies f_min = 1/T: a 3 ms window ⇒ 333 Hz; a 3.54 ms window ⇒ 282 Hz; below that, curves are "an artifact of the Fourier transform," and lower crossover/higher order ⇒ longer required gate.

**Confirm or refute:** *"No pulsed scheme can create an echo-free lull because the DUT's response to ANY excitation persists past τ."* **CONFIRMED.** The reflection is the DUT's own emitted waveform delayed by τ = 0.31 ms and scaled by r; since the DUT's response to any excitation extends past 0.31 ms (LR4 all-pass + horn), the delayed copy necessarily overlaps the still-decaying direct response. There is no waveform for which the pressure at the mic is "direct-only" for a full 0.31 ms and then cleanly silent.

**(b) Shipped-product vs academic evidence.**

- *Academic/standards*: Struck & Temme "Simulated Free Field Measurements" (AES 93rd Conv. 1992, paper 3397 / JAES 42(6):467–482, 1994) — near-field/far-field splice, gating limits; D'Appolito CLIO app note (f_min = 1/T). Crossover group-delay/impulse behavior: Linkwitz "Active Crossover Networks for Noncoincident Drivers" (JAES 1976); textbook LR all-pass summation.
- *Shipped-product*: REW, ARTA, CLIO, Klippel documentation all implement 1/T window-limit warnings and near-field splicing precisely because the DUT tail + gate trade-off is unavoidable. REW's help states: "if the length of signal we analyze is T seconds, the lowest frequency is 1/T — so if our window was only 3 ms long, the frequency response would only go down to 1/0.003 = 333 Hz."

**(c) Citations.** Struck & Temme 1994; D'Appolito/Audiomatica CLIO note; Linkwitz 1976; REW/ARTA manuals.

**(d) Confidence + contested points.** **Very high.** Minor nuance: the *magnitude* of the LR4 group-delay peak depends on the acoustic (not just electrical) slopes actually realized by driver+horn+DSP; the exact tail length is DUT-specific, but that it exceeds 0.31 ms is not in doubt.

**(e) Design consequence.** Any 0.31 ms gate must be treated as an HF-only (>3.2 kHz) diagnostic and level-corrected/spliced; never report gated magnitude below ~3.2 kHz as calibrated.

-----

### QUESTION 3 — Prior art on time-selective and burst excitation

**(a) Consensus answer.** A survey of the burst/time-selective family shows each technique solves a *different* problem than reflection-lull-excision:

- **Tone-burst (shaped sine burst) measurements**: historic anechoic substitute and today used for power handling and for burst-decay resonance analysis (ARTA burst decay; constant relative bandwidth Δf/f via N-cycle Gaussian-shaped bursts). Solves: resonance/decay visualization and level/power tests — not early-reflection rejection.
- **Cumulative Spectral Decay (CSD)/waterfall** (origin: D.E.L. Shorter, BBC, 1940s) and **wavelet/Wigner-Ville/burst-decay** (Keele-style CSD alternatives; Listen SoundCheck TFA; Klippel TFA): time-frequency views of the *DUT's own* decay. Low-frequency limit = inverse of window duration — same 1/T wall. Solves: resonance diagnosis, not reflections.
- **MLS/periodic excitation** (Rife & Vanderkooy): full-period IR recovery; reflections handled afterward by windowing. No lull.
- **Interrupted-noise / silence-gap methods**: used in room-acoustics reverberation-time (RT60) estimation (excite, cut source, watch decay). The "silence gap" measures *room decay*, the opposite of rejecting a single early reflection.
- **In-situ quasi-anechoic tricks**: ground-plane (make mic+source part of the boundary so the floor bounce lands *on* the mic, eliminating the separate reflection; +6 dB coherent gain, measure at 2× distance); dual-height/subtraction (Mommertz 1995; Robinson & Xiang 2010) — subtract a reference (direct-only) response to isolate the reflection; near-field + far-field splice (Struck & Temme); cepstral echo detection/removal (Bolton & Gold 1984/1986; Holland 1993).

**Is any a precedent for lull-excision?** **No.** The nearest relatives are (i) ground-plane, which *geometrically* removes the reflection rather than time-gating it, and (ii) cepstral liftering, which removes a comb by editing the cepstrum (a model-ish operation, closer to Question 4). None emits a burst train timed so the echo falls in a silent gap; and per Question 2 such a gap cannot be echo-free anyway.

**(b) Shipped-product vs academic evidence.**

- *Shipped-product*: Listen SoundCheck (burst decay, CSD, wavelet, splice sequences); Klippel TFA (STFT/CSD/wavelet) and NFS (holographic near-field field-separation — a genuinely different, array-like reflection removal); ARTA/CLIO/REW (gating + near-field splice). Ground-plane is standard field practice.
- *Academic*: Rife & Vanderkooy (MLS, JAES 1989); Bolton & Gold cepstral reflection coefficients (JSV 1984, 1986); Struck & Temme 1994; interrupted-noise RT in ISO 3382.

**(c) Citations.** Shorter (BBC, 1940s); Rife & Vanderkooy 1989; Bolton & Gold 1984/1986; Mommertz 1995; Robinson & Xiang 2010; Struck & Temme 1994; Klippel NFS/TFA docs; Listen SoundCheck TFA docs.

**(d) Confidence + contested points.** **High** that no burst/lull-excision reflection-rejection precedent exists in the literature or products surveyed; this is an argument from a thorough but not exhaustive search, so treat as "absence of evidence" rather than proof of absence.

**(e) Design consequence.** Cite ground-plane and cepstral liftering as the real precedents for "get rid of the early bounce"; do not market a burst-gap method as novel-but-proven — it has no track record and is theoretically dominated by ordinary gating.

-----

### QUESTION 4 — The loopholes: what CAN beat the Fourier limit at one point?

**(a) Sparse/parametric super-resolution.**
*Theory*: Modeling h(t) as K weighted Diracs (direct + 1–3 reflections) makes it a finite-rate-of-innovation (FRI) signal recoverable from 2K samples independent of bandwidth (M. Vetterli, P. Marziliano & T. Blu, "Sampling Signals with Finite Rate of Innovation," IEEE Trans. Signal Process. 50(6):1417–1428, June 2002; T. Blu, P. L. Dragotti, M. Vetterli, P. Marziliano & L. Coulot, "Sparse Sampling of Signal Innovations," IEEE Signal Processing Magazine 25(2):31–40, March 2008 — explicitly framed as "going around" the uncertainty limit via a parametric prior). Subspace methods (MUSIC/ESPRIT) and matrix-pencil/Prony give exact recovery in the noiseless case regardless of separation, and empirical super-resolution below 1/bandwidth when SNR is high enough.

*What is actually demonstrated, and at what SNR*: The clean sub-1/bandwidth numbers are largely **simulation** with **known K and constant scalar amplitudes**. Representative results: two equal-amplitude Diracs reach the Cramér–Rao bound for SNR ≳ 15–20 dB *provided their separation exceeds a "no-breakdown" distance*; below it a subspace swap merges the two arrivals into one (Wei & Dragotti, ICASSP 2017). Multichannel FRI resolves close delays (Δ ≈ 0.086·T) at SNR > 15 dB with 4 channels (Gedalyahu, Tur & Eldar, IEEE TSP 2011). The most relevant *acoustic* study (Deppisch, Amengual Garí, Calamia & Ahrens, "Direct and Residual Subspace Decomposition of Spatial Room Impulse Responses," IEEE/ACM TASLP 31:927–942, 2023) sweeps reflection TDOA from 0 to 0.5 ms but explicitly designs its subtraction for reflections separated by ≥1 ms and uses a **spherical microphone array**, flagging the sub-1 ms/simultaneous case as the hard, unsolved one. **No measured single-microphone ~0.31 ms direct/reflection super-resolution at 30–50 dB SNR was found** — this is an evidence gap; the enabling demonstrations use extra channels or larger separations.

*Does r(f) break the constant-attenuation assumption?* **Yes, in principle, and this is the crux.** MUSIC/ESPRIT/FRI assume each echo contributes a single constant complex scalar a_k·e^{-j2πf t_k}; the annihilating-filter/low-rank structure requires it. A reflection whose relative amplitude varies with frequency (loudspeaker directivity into the bounce angle + angle-dependent boundary absorption) is, in the transform domain, a *frequency-varying amplitude* — precisely the model mismatch shown to bias and inflate the variance of MUSIC/ESPRIT frequency estimators (published robustness analyses of subspace estimators under time-varying amplitude derive closed-form bias/variance and show degradation grows with envelope bandwidth). The data matrix ceases to be exactly rank-K and Cadzow/low-rank denoising mis-projects. Frequency-dependent reflection is real and documented (directivity + uneven absorption); the exact acoustic quantification "does r(f) defeat FRI for a loudspeaker RIR" is **not** in the literature — a genuine gap, so this is a defensible inference, not a cited result.

*Could a HYBRID work (fix τ from the cepstral detector, then jointly fit smooth H_d(f) and smooth r(f))?* This is exactly the two-path model H_meas(f) = H_d(f)·[1 + r(f)·e^{-j2πf·τ}]. Prior art with this *shape* exists but not your exact single-measurement inversion:

- Bolton & Gold (JSV 1984; JSV 1986, Parts I & II) use cepstral techniques to separate direct from reflection and extract a **frequency-dependent** reflection coefficient; Part II adds "cepstral inversion" for surfaces whose IR is longer than τ. **Key limitation**: the basic method needs the surface IR shorter than τ, and "extraction noise" is set by the loudspeaker source spectrum — i.e., it degrades exactly when τ is small (~0.31 ms) and the source is not flat.
- Mommertz (Applied Acoustics 46(3):251–263, 1995) and Robinson & Xiang (JASA 127(3):EL99–EL104, 2010) measure complex, frequency- and angle-dependent r(f) by **subtracting a reference direct-only response** — they model r(f) but require a separate reference and target larger τ; residual direct sound from misalignment is their dominant error.
- Ultrasonics analogue: modeling the ratio of two echoes as a **rational transfer function** to estimate continuous frequency-dependent attenuation/velocity, with up to ~50× lower variance than non-parametric Fourier methods — the clearest example of "invert for smooth frequency-dependent reflection response instead of a scalar," but not room acoustics.
  **No paper does exactly your proposal** (single-mic loudspeaker RIR, fix τ from cepstrum, jointly invert for smooth H_d(f) and smooth r(f) at ~0.31 ms and consumer SNR). It is a plausible, novel combination — and the regularization (smoothness of H_d and r) is what buys back the identifiability that a free K-Dirac fit loses when r varies with frequency.

**(b) Directional discrimination (compact arrays / Trinnov tetrahedron).**
Trinnov's 4-capsule tetrahedron localizes sources and "detects early reflection provenance" via arrival-time differences across capsules of known geometry. Per Trinnov's published spec, each capsule is factory-calibrated flat to within ±0.1 dB from 20 Hz to 24 kHz, and the array achieves 3D localization "accurate to 2° of azimuth, 2° of elevation, and less than 1 cm of distance." But resolving two arrivals ~14° apart in *angle* at a single point is aperture-limited by Rayleigh: Δθ ≈ λ/D. To resolve 14° (≈0.24 rad) you need aperture D ≈ λ/0.24. At 3 kHz (λ ≈ 11.4 cm) that is D ≈ 47 cm — far larger than a few-cm tetrahedron; at low frequencies it is hopeless. A compact tetrahedron (side ~a few cm) gives inter-capsule TDOA maxima of only ~tens of microseconds (e.g. 3 cm ⇒ 87 µs), comparable to a 48 kHz sample period, so it discriminates *broadband direction* (for speaker localization) but cannot cleanly separate a 0.31 ms early reflection from the direct sound in the *magnitude response* at low frequencies. Trinnov's own 2°/1-cm figures are *localization* accuracy for well-separated broadband sources, not evidence of magnitude-response separation for a 0.31 ms echo. **Minimum geometry to separate 14°-apart arrivals with useful resolution across the audio band is tens of cm of aperture** — i.e., not a compact consumer array, and it needs multiple synchronized capsules (extra channels). This is a spatial-channel escape, not a single-point one.

**(c) Moving-mic/Doppler (plenacoustic / dynamic RTF).**
Ajdler, Sbaiz & Vetterli ("Dynamic measurement of room impulse responses using a moving microphone," JASA 122(3):1636–1645, 2007; "The Plenacoustic Function and Its Sampling," IEEE Trans. Signal Process. 54(10):3790–3804, Oct. 2006) reconstruct a continuum of RIRs from one continuously moving mic; the moving-mic response has a "butterfly" spectrum (spatial bandwidth grows with temporal frequency). Recent kernel-ridge/dynamic-RTF work extends this. **Mechanistically this is spatial diversity acquired in one session, not a new single-point separation principle** — it is your discrete multi-position spatial averaging taken to the continuous limit. It can improve spatial coverage/robustness but does not, at a *single* point, break the time-bandwidth limit; and it introduces motion/Doppler and synchronization complications.

**(d) Confidence + contested points.** **High** that super-resolution is the only single-channel escape and that it is unproven for your exact case; **high** that arrays/moving-mic are spatial-channel escapes. **Contested/open**: whether a regularized r(f)-aware two-path inversion can work at 30–50 dB for τ = 0.31 ms — genuinely unknown, no direct prior art either way.

**(e) Design consequence.** If you pursue a single-point escape, it must be the r(f)-aware regularized two-path inversion seeded by your measured τ — not a plain K-Dirac MUSIC/ESPRIT fit, which the frequency-dependent bounce will bias.

-----

### QUESTION 5 — Verdict framework

Ranking the four candidates, given spatial power averaging already ships:

|Rank |Candidate                                                          |Theoretical soundness                                       |Demonstrated prior art                             |Consumer-HW feasibility (phone/USB mic)       |Marginal value on top of spatial averaging                                                                         |
|-----|-------------------------------------------------------------------|------------------------------------------------------------|---------------------------------------------------|----------------------------------------------|-------------------------------------------------------------------------------------------------------------------|
|**1**|**(ii) Ultra-short-gate HF cross-check on existing sweeps**        |High — it *is* gating; honest above ~3.2 kHz                |Extensive (every measurement suite)                |Trivial/zero-cost (reuse existing sweep)      |Modest but real: independent HF direct-sound check, comb-detection QA                                              |
|**2**|**(iii) Hybrid parametric two-path inversion seeded by measured τ**|Medium-high *if* r(f) modeled and regularized               |Partial (cepstral/subtraction r(f); no exact match)|Feasible in DSP, single mic; compute-only     |Potentially high sub-3.2 kHz — the one thing that could actually recover direct-path magnitude where the comb lives|
|**3**|**(iv) Moving-mic capture**                                        |Sound but = spatial diversity, not single-point             |Strong (Ajdler/Vetterli)                           |Feasible but adds motion/UX + Doppler handling|Low — duplicates what your 8–12 position averaging already does                                                    |
|**4**|**(i) Pulsed lull-excision**                                       |Sound only as a re-derivation of gating; no lull exists (Q2)|None                                               |Feasible but pointless                        |~Zero — strictly dominated by (ii); adds cost, no new information                                                  |

**Recommendation order**: build (ii) now (zero cost); prototype (iii) as a bounded research bet; ignore (i); treat (iv) as optional coverage, not a separation method.

**Cheapest experiment that would falsify the top-ranked candidate (ii):** Take existing sweeps you already have from a position with the known τ = 0.31 ms echo, apply a 0.29–0.31 ms gate, and compare the resulting >3.2 kHz magnitude against (1) your spatial-average result and (2) a trusted reference (ground-plane or anechoic/NFS) for the same speaker. If the short-gate HF response contains no information that spatial averaging above 3.2 kHz doesn't already provide (and it cannot recover anything below 3.2 kHz — you already showed the level bias), then (ii)'s marginal value is nil and it should be demoted to a pure QA/consistency check rather than a measurement method. This costs only reprocessing of data in hand.

-----

## Recommendations

1. **Kill variants A and B as measurement methods now.** They are mathematically a 0.31 ms gate / TDS and cannot reach below ~3.2 kHz. Document the equivalence internally (Vanderkooy 1986; Müller & Massarani 2001) so the idea is not revisited. *Threshold to revisit: none — this is a theorem, not an empirical result.*
1. **Ship the ultra-short-gate HF cross-check (ii) as zero-cost QA.** Reuse existing sweeps; gate ~0.3 ms; report only >~3.2 kHz, level-corrected; use it to independently confirm the direct-sound HF balance and to flag comb contamination. *Benchmark: agreement with spatial-average HF within your target tolerance (e.g. ±1 dB, 1/3-oct) above 3.2 kHz.*
1. **Prototype the r(f)-aware regularized two-path inversion (iii) offline before committing.** Fix τ from your cepstral detector; fit H_meas(f) = H_d(f)·[1 + r(f)e^{-j2πfτ}] with smoothness priors on both H_d and r. Validate against synthetic two-path data with a *frequency-dependent* r(f) at 30, 40, 50 dB SNR, then against ground-plane/NFS ground truth. *Go/no-go thresholds: if it cannot recover H_d below 3.2 kHz to within ~1–1.5 dB at ≥40 dB SNR with realistic r(f), shelve it; if it can, it is the only method that meaningfully improves on spatial averaging in the comb region.*
1. **Do not invest in compact-array or moving-mic separation for this problem.** A tetrahedron cannot resolve the 14° / 0.31 ms case across the band (needs ~tens of cm aperture and extra channels); moving-mic is your existing spatial averaging in continuous form. *Revisit only if you add genuine multi-capsule hardware.*
1. **Keep spatial power averaging as primary; treat any single-point method as complementary.** The single-point escapes either don't beat the limit (i, ii below 3.2 kHz), duplicate averaging (iv), or are unproven (iii).

-----

## Caveats

- **"No shipped product uses pulsed lull-excision" is an absence-of-evidence finding**, not a proof; patents/products in this space (Sonos Trueplay spectral correction, Dirac, Audyssey MultEQ, Anthem ARC, Genelec GLM, Trinnov, Klippel NFS) universally rely on spatial averaging, near-field/holographic field separation, or post-hoc windowing/cepstral detection — none on burst-gap excitation — but a negative literature result can be overturned by a single obscure patent.
- **The LR4 group-delay/tail magnitude is DUT-specific.** That the combined LR4-plus-horn response exceeds 0.31 ms is certain; the exact tail length depends on the realized acoustic slopes and horn behavior and should be measured on your unit.
- **All clean super-resolution resolution-vs-SNR curves are simulations** with known K and constant scalar amplitudes; the acoustic, single-mic, r(f)-varying, ~0.31 ms, consumer-SNR case is unproven in either direction. Claims about (iii) working are hypotheses to be tested, not established results.
- **r(f) as an FRI/subspace failure mode is an inference** from general subspace-robustness theory plus the physical reality of directivity/absorption; no paper quantifies it for a loudspeaker RIR specifically.

-----

## Grading of pre-registered predictions

- **P1 — CONFIRMED.** Both variants equal a ~0.31 ms gate for LTI systems; variant B is Heyser's TDS, and TDS = gating is established (Vanderkooy 1986; Müller & Massarani 2001 state the TDS low-pass is equivalent to IR windowing). No excitation design escapes the time–bandwidth limit for single-channel LTI without added model/spatial assumptions; the cost of separating 0.31 ms arrivals is ~1/τ ≈ 3.2 kHz, rendering below ~3.2 kHz unreachable. The TDS derivation reaches the identical bound (√(sweep rate) ≳ 1/τ). Fully confirmed.
- **P2 — CONFIRMED.** The DUT's own IR (LR4 all-pass group-delay peak of hundreds of µs at 2 kHz + compression-horn delay/ringing) persists past 0.31 ms, so the echo of the early response overlaps the direct tail; no true lull exists under any excitation. Consistent with your 0.29 ms-gate experiment (comb removed, level biased down several dB by truncating decay). Confirmed; the only nuance is that the exact tail length is DUT-specific.
- **P3 — CONFIRMED (absence-of-evidence).** No shipped product uses pulsed lull-excision; burst/tone-burst methods exist only for power handling, burst-decay/CSD resonance analysis, and interrupted-noise RT — none is a reflection-rejection lull-excision precedent. Confirmed subject to the caveat that this is a negative search result.
- **P4 — CONFIRMED.** Model-based super-resolution (sparse/parametric arrival separation) is the only theoretically sound *single-point, single-channel* escape, and it is unproven at consumer SNR with frequency-dependent reflection strength: demonstrations are largely simulated with known K and constant amplitudes, no measured single-mic ~0.31 ms case at 30–50 dB was found, and r(f) provably biases the subspace/FRI estimators. (Directional arrays and moving-mic are escapes too, but they are *spatial-channel* methods, not single-point — so P4's "only … single-point escape" stands.)
