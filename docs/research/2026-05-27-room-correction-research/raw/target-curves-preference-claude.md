# JTS Target Curves & Preference Tuning — Research Report

## Executive Summary

For an open-source smart speaker built on Raspberry Pi 5 + CamillaDSP, the strongest defensible architecture is a **strict three-layer DSP separation**: (1) deterministic, bounded room/speaker correction restricted to the modal region below the room's transition frequency (typically 200–300 Hz per Toole, *JAES* 63(7/8), 2015); (2) a small, parameterised house/target curve layer (bass shelf + tilt + optional treble shelf) seeded from the Harman/Olive in-room research (~6 dB bass shelf at ~105 Hz, ~−1 dB/octave overall tilt) but exposed as a few user-friendly knobs; and (3) a reversible "voicing" preference EQ layer with hard safety bounds (±6 dB shelves, ±3 dB tilt, headroom auto-reservation) that an AI assistant can manipulate symbolically but never authors filter coefficients directly. This pattern matches the layering used implicitly by Genelec GLM (AutoCal → Sound Character Profiler), Neumann MA 1 (room-adaptive target → manual tweak), Sonarworks SoundID Reference (Flat Target → Custom Target → Translation Check), Dirac Live (Auto Target Curve → user handles), and Roon (Convolution / Room Correction → Parametric EQ → Headroom Management).

The single most important research-backed constraint is Toole's transition-frequency rule (JAES 2015, pp. 515 & 517): minimum-phase EQ corrections to the steady-state in-room curve above ~300 Hz mostly chase non-minimum-phase reflection artefacts that two ears and a brain already perceptually separate, so JTS room correction should default to a 20–500 Hz active range with a soft taper to ~1 kHz. Above that, target-curve tilt and preference EQ are the *only* legitimate manipulations.

Listener preference is consistent enough to ship a single neutral default (the "Harman/Olive class 1" curve captures ~64% of listeners; Olive, *Acoustics Today* Spring 2022), but split enough that JTS should expose a small bass-trim and tilt control: Olive & Welti (AES Conv. Paper 9382, 2015) and Olive's follow-up cluster analysis show 15% of listeners (skewed young/male) want +4 to +6 dB more bass, and 21% (skewed older/female) want −2 dB less bass.

---

## TL;DR

- **Architecturally separate three layers** (room correction ≤ ~500 Hz; target curve = bass shelf + tilt + treble shelf; reversible preference EQ with hard limits). This mirrors every credible prosumer/pro tool and aligns with Toole's JAES 2015 transition-frequency argument: "Below the transition/Schroeder frequency equalization has a role to play" (p. 517); above it, peaks and dips are "non-minimum-phase phenomena that are not correctable by minimum-phase equalization."
- **Seed defaults from Olive/Welti**: an in-room tilt of about −1 dB/octave (Olive, *Acoustics Today* 2022: "the in-room response gently falls about 1 dB per octave from 20 Hz to 20 kHz") and a 3–6 dB low shelf at ~105 Hz (Welti interview citing AES Conv. Paper 8994, 2013) cover the majority preference; ship a "bass −2 / 0 / +3 / +6 dB" preset family to cover the three documented preference classes.
- **Preference EQ must be deterministic and bounded** (±6 dB shelves, ±3 dB peak, Q 0.3–4.0, auto-reserved headroom equal to max positive gain + 1 dB), versioned JSON, with one-tap A/B against bypass/target-only/full-chain and a parent-linked rollback history. An LLM assistant may *propose* an intent ("more punch" → +2 dB shelf at 80 Hz, Q 0.7) but the deterministic compiler clamps and writes the CamillaDSP YAML.

---

## Key Findings

### 1. Preferred in-room target curves — what the literature actually says

**Harman / Olive / Welti work.** Sean Olive's research at Harman established that listeners in controlled double-blind tests reliably prefer loudspeakers whose anechoic on- and off-axis responses are smooth, flat, and well-controlled; both trained and untrained listeners produce the same rank ordering, though trained listeners are 3–20× more discriminating and reliable (Olive, "Differences in Performance and Preference of Trained Versus Untrained Listeners in Loudspeaker Tests," *J. Audio Eng. Soc.* 51(9):806–825, Sept 2003). For the *in-room target* specifically, Olive, Jackson, Devantier, Hunt & Hess ("The Subjective and Objective Evaluation of Room Correction Products," AES Conv. Paper 7960, 127th Conv., 2009) found, as Olive summarised on his blog: "The preferred room corrections have a target response that has a smooth downward slope with increasing frequency. This tells us that listeners prefer a certain amount of natural room gain. Removing the room gain makes the reproduced music sound unnatural, and too thin." Olive (*Acoustics Today*, Spring 2022) summarises the resulting curve as "the in-room response gently falls about 1 dB per octave from 20 Hz to 20 kHz."

The most precise quantitative preference data come from Olive, Welti & McMullin ("Listener Preferences for In-Room Loudspeaker and Headphone Target Responses," AES Conv. Paper 8994, 135th Conv., Oct 2013). The paper abstract reports: "Listeners on average preferred an in-room loudspeaker target response that had 2 dB more bass and treble compared to the preferred headphone target response. There were significant variations in the preferred bass and treble levels due to differences in individual taste and listener training." Welti himself summarised the magnitudes in a HomeTheaterHifi interview: "Subjects were allowed to adjust shelving filters in real time… The filters were: a bass shelving filter at 105 Hz, and a treble 'tilt' filter at and above 2.5 kHz. There was a fair amount of variation from subject to subject, but if you averaged it all out, you get a bass boost of around 6 dB and a gently rolling off high end, around −2.5 dB at 10 kHz." Follow-up work (Olive & Welti, "Factors That Influence Listeners' Preferred Bass and Treble Levels in Headphones," AES Conv. Paper 9382, 139th Conv., 2015) identified three preference classes (see §2).

**Floyd Toole's framing.** Toole, "The Measurement and Calibration of Sound Reproducing Systems," *JAES* 63(7/8):512–541, July/Aug 2015 (DOI 10.17743/jaes.2015.0064), is the canonical text. He frames the room curve not as a target but as a *result* (AVForums comments, 2017): "There isn't, nor can there be, an ideal steady-state 'target' room curve. The room curve is a 'result' of a loudspeaker delivering sound to a complex semi-reflective listening environment, it is not a 'target'." The actionable rules from JAES 2015 are:

- "Below the transition/Schroeder frequency, around 300 Hz here, the room is the dominant factor; above it, the loudspeaker is substantially in control." (p. 515)
- "It is essential to separate events above and below the transition/Schroeder frequency. Above it… These are non-minimum-phase phenomena that are not correctable by minimum-phase equalization… human listeners find these multi-directional reflected sounds to be mostly benign, even beneficial if the loudspeaker has relatively constant directivity." (p. 517)
- "Below the transition/Schroeder frequency equalization has a role to play." (p. 517)
- "Equalization cannot repair faulty loudspeaker directivity." (p. 532)

For small rooms, Toole's *Sound Reproduction*, 3rd ed. (Routledge, 2018), Ch. 7 ("Above the Transition Frequency: Acoustical Events and Perceptions") puts the transition frequency "somewhere in the region of 200 to 300 Hz." The Schroeder formula `f_S = 2000·√(T60/V)` is, as Toole notes, strictly applicable to large reverberant spaces; in small living rooms the practical transition is the empirical modal/statistical boundary at ~200–500 Hz.

**B&K 1974 curve.** The Brüel & Kjær 1974 research (commonly transcribed as: 0 dB @ 20 Hz, ~+3 dB @ 70–100 Hz, 0 dB @ 1 kHz, −3 dB @ 10 kHz, −6 dB @ 20 kHz; ≈ −0.9 dB/octave above 500 Hz) was derived from in-room measurements of well-regarded hi-fi systems in domestic rooms. Sonarworks ships it as a "Translation Check → Other" preset labelled "B&K 1974 Speaker Target" in SoundID Reference, alongside X-Curve and 2/4/6 dB Tilt presets. It and the Harman/Olive curves are similar in shape (gentle downward tilt + mild bass lift) and differ mainly in the steepness of the HF roll-off.

**Additional AES papers on preferred in-room response.** Olive, "A Multiple Regression Model for Predicting Loudspeaker Preference Using Objective Measurements," Parts I & II (AES Conventions 116 \& 118, 2004); Welti & Devantier, "Low-Frequency Optimization Using Multiple Subwoofers," *JAES* 54(5):347–364 (May 2006); Welti, "Optimal Configurations for Subwoofers in Rooms Considering Seat to Seat Variation and Low Frequency Efficiency" (AES Conv. Paper 8748, 133rd Conv., 2012). For modal control specifically: Mäkivirta, Antsalo, Karjalainen & Välimäki, "Modal Equalization of Loudspeaker-Room Responses at Low Frequencies," *JAES* 51(5):324–343 (May 2003).

**Headphone Harman target (2013/2015/2018 revisions).** Olive & Welti (AES Conv. Paper 8740, 133rd Conv., Oct 2012, "The Relationship Between Perception and Measurement of Headphone Sound Quality") established the methodology — equalising a calibrated reference loudspeaker into a GRAS 45 CA fixture. The 2015 revision (AES 9382) added the bass/treble preference shifts; the 2018 in-ear revision (Olive, Welti & Khonsaripour, "A Statistical Model that Predicts Listeners' Preference Ratings of In-Ear Headphones, Parts 1 & 2," AES Conv. Papers 9840 & 9878, 143rd Conv., 2017) delivered an in-ear target with reported 91% correlation between preference rating and frequency-response features. As of ~2020 Harman re-measures on the B&K Type 5128 HATS; old GRAS-based targets do not transfer directly (Olive, "A Comparison of In-Ear Headphone Target Curves for the Brüel & Kjær Head & Torso Simulator Type 5128").

### 2. Listener preference variation — strongest findings

- **Bass preference clusters (Olive/Welti).** Olive (*Acoustics Today*, Spring 2022, Table 1, summarising headphone latent-class analysis) yields three classes: **Class 1 ("Harman Lovers")**: 64% — broad demographic, prefer the mean target; **Class 2 ("More Bass Is Better")**: 15% — disproportionately young and male, prefer +4 to +6 dB more bass below ~300 Hz and ~+1 dB above 1 kHz; **Class 3 ("Less Bass Is Better")**: 21% — disproportionately older and female, prefer −2 to −3 dB less bass and ~+1 dB more treble.
- **Multi-subwoofer bass variation (Welti & Devantier, *JAES* 54(5), 2006; Welti AES 8748, 2012).** Seat-to-seat bass variance is driven by room modes; 4 subwoofers at wall midpoints reduce mean spatial variance (MSV) to ~1–2 dB better than 4-corner placement. The implication for JTS, a single-cabinet speaker: room correction alone cannot fix bass at multiple positions; multi-position measurement averaging is essential and the system must transparently disclose that the result is an average, not a per-position fix.
- **Room size/liveness.** Toole, *Sound Reproduction*, Ch. 7: in untreated rooms the in-room steady-state curve naturally exhibits ~0.5–1 dB/oct droop simply because of HF air/material absorption. Forcing a flat in-room target therefore *boosts* treble unnaturally (Dirac, "What is the best target curve for room correction?"; Toole 2015 throughout).
- **Speaker directivity.** Toole (JAES 2015 p. 532): "Equalization cannot repair faulty loudspeaker directivity." Constant-directivity speakers produce smoother, more predictable in-room curves and tolerate less aggressive correction; wide-directivity speakers benefit from more bass lift to compensate for higher reflected energy in the upper bass.
- **Age and hearing.** Olive (blog post, Nov 2015, summarising AES 9382): "Listeners over 55 years preferred less bass and more treble than the younger listeners suggested that they were compensating for possible hearing loss that is associated with increased age." Female listeners on average preferred ~1 dB more bass and ~2 dB more treble than males (Olive's wording); small female sample limits confidence.
- **Trained vs untrained.** Olive 2003 (*JAES* 51(9):806–825): rank order of preference is the same, but trained listeners are 3–20× more discriminating and produce tighter F-statistics. Untrained listeners often "choose more of everything" (Toole 2015 commentary on Olive/Welti/McMullin 2013) — which has direct UX implications: an untrained user adjusting JTS via sliders will tend to add too much bass and treble both; bounded sliders and an explicit "reset to neutral" button are protective.
- **Cultural/regional.** Olive, *Acoustics Today* Spring 2022, p. 61: "A total of 283 listeners participated from four different countries (Canada, United States, Germany, and China)… headphone preferences were remarkably consistent across the 11 test locations for both trained and untrained listeners." No strong evidence of culturally distinct curve preferences in the peer-reviewed literature.

### 3. DSP architecture — how the field separates correction, target, and preference

| Tool | Room correction | Target curve | Preference / voicing |
|---|---|---|---|
| **Dirac Live** | Mixed-phase FIR over user-defined frequency range; sub-bass to full-range | Legacy static "Curve 1" default is a straight −0.5 dB/oct tilt 20 Hz–20 kHz; March 2022 "Auto Target Curve" is measurement-adaptive — per Dirac (2022 press release): it "more faithfully reflects the inherent character of the measured system, minus the adverse acoustic effects of the room" | Same target editor; no separate preference layer; Bass Control add-on for sub integration |
| **Sonarworks SoundID Reference** | Linear-phase or mixed-phase correction to "Flat Target" (SoundID SR) | Flat / Dolby / B&K 1974 / X-Curve / Tilt (2/4/6 dB) | "Custom Target" parametric EQ on top of corrected response; "Translation Check" simulates other devices |
| **Genelec GLM 5** | AutoCal IIR on monitor DSP, focused on bass-region resonances | Implicit (anechoic-flat target); user adjusts via "Sound Character Profiler" | Sound Character Profiler global EQ + bass tilt/roll-off DIP/digital |
| **Neumann MA 1** | Fraunhofer IIS algorithm, "room-adaptive target curve"; bass and bass-management focused | "MA 1 calculates the room-specific target curve"; user can modify | Manual tilt within target curve editor; multiple presets per setup |
| **Audyssey MultEQ** | FIR full-range, several modes (Reference / Flat / L/R bypass) | Reference adds HF roll-off to Flat | Dynamic EQ implements an equal-loudness compensation referenced to MultEQ XT calibration level (cinema reference 0 dB master volume); offsets are documented (Denon/Marantz AVR manuals; Ask Audyssey portal) as "three offsets from the film level reference (5 dB, 10 dB, and 15 dB)" — 0 dB = film, 5 dB = mixed content, 10 dB = TV, 15 dB = "pop/rock music or other program material that is mixed at very high listening levels" |
| **Roon (MUSE DSP)** | Convolution slot (BYO room filter); Procedural EQ | Manual via Parametric EQ shelves | Same Parametric EQ; Headroom Management enforces clipping safety |
| **WiiM Home + RoomFit** | Up to 10-band PEQ per channel (current firmware; WiiM FAQ: "The Per-Source Parametric EQ supports up to 10 bands for each input source"); RoomFit and EQ are now separate layers per WiiM community post (Aug 2025: "you can use all 10 bands for RoomFit and another 10 bands for PEQ now that RoomFit and EQ are separate") | Flat / B&K / Harman selectable | Same PEQ slots; users manually edit |
| **HouseCurve (iOS)** | Sweep → auto PEQ fit | Importable target curves; presets for Harman, B&K, flat | None (uses same PEQ on destination device) |
| **miniDSP** | Per-input/output IIR or Dirac Live add-on | Whatever user imports | Same biquad slots |
| **AutoEQ / JamesDSP / Equalizer APO + PEACE** | Filter generation from measured FR vs target | Configurable target curves (Harman, custom) | Manual GUI tone controls (PEACE), preset stacks |
| **CamillaDSP** | YAML-defined biquad/FIR chains; user-authored | Up to the front-end app (HiFiBerry, moOde, custom) | Same; no built-in semantic layer |

**Architectural pattern.** Every credible system separates measurement-driven correction from target shape, and exposes target as parameters (shelf, tilt, roll-off) rather than as raw EQ. Roon and Dirac additionally surface explicit headroom management whenever the user adds positive gain. CamillaDSP gives JTS exactly the primitives needed (Biquad: Peaking, Lowshelf, Highshelf, Highpass, Lowpass, Linkwitz-Riley combos; Conv FIR; Gain; Delay; Mixer; Pipeline) but no semantic structure — that is for JTS to invent.

### 4. Target-curve parameters — recommended exposure

Anchored to research:
- **Bass shelf**: gain 0 to +9 dB (default +3 dB; "Harman class 1" mean is ~+4 dB at the listening position once room gain is included); corner frequency 60–200 Hz (default 105 Hz per Olive/Welti/McMullin AES Conv. Paper 8994, 2013); Q (slope) 0.5–0.7.
- **Tilt**: −1.5 to +0.5 dB/octave from 100 Hz to 10 kHz (default −0.5 to −1.0 dB/oct; Dirac's legacy default = −0.5 dB/oct; Olive 2022 mean ≈ −1.0 dB/oct).
- **Treble shelf**: −6 to +3 dB above 2–5 kHz (default 0 dB).
- **Correction range** for room correction stage: 20–500 Hz active, soft taper 500–1000 Hz, identity above (per Toole 2015 p. 517; Genelec and Neumann both limit broadband correction in practice; Audyssey "Flat" mode is the closest pro analogue).
- **Maximum boost/cut for correction**: ±6 dB cut, +3 dB boost (matches Dirac defaults and the conservative practice promoted by Audio Science Review's measurement community; aggressive boost of narrow dips chases phase artefacts).
- **Loudness compensation (optional)**: ISO 226:2003 equal-loudness contours, referenced to a calibrated reference SPL (cinema = 85 dB SPL pink noise per channel; music typically 75–82 dB). Audyssey Dynamic EQ implements precisely this concept with the Reference Level Offsets quoted above. For JTS, recommend a switchable loudness layer (off by default) that scales bass and (lightly) treble lift as a function of measured replay SPL minus a reference, with caps of +6 dB at 50 Hz and +3 dB at 10 kHz at 50 dB SPL replay.

### 5. Subjective language → EQ intent — research-backed mappings

Anchored to Pedersen & Zacharov, "The Development of a Sound Wheel for Reproduced Sound" (AES Conv. Paper 9310, 138th Conv., May 2015) and the FORCE Technology / DELTA SenseLab audio-wheel vocabulary; Toole *Sound Reproduction* Ch. 19; the Stereophile audio glossary (a widely cited descriptive lexicon that uses Toole-era language). Comprehensive table in §"Mapping table" below.

### 6. Avoiding correction-vs-voicing confusion

Three practices keep them distinct:
1. **Frequency-range gating.** Room correction filters live in 20–500 Hz only (with a soft 500–1 kHz taper). The user cannot point room correction at the 4 kHz "harsh" region; that complaint can only be answered by target curve / preference EQ.
2. **Provenance tagging.** Every filter in the chain carries a `source` tag in JSON (`"room"`, `"target"`, `"preference"`, `"loudness"`). Bypass-by-layer is exposed in the UI; the design-audit report lists filters by provenance.
3. **Schroeder/transition-frequency disclosure.** The audit report should compute and state the room's transition frequency (estimated from measured T60 and volume, or by inflection of the SD-vs-frequency curve) and refuse to author room-correction filters above it (with override + warning, mirroring how Dirac and Audyssey will EQ broadband but Genelec/Neumann conservatively keep AutoCal narrow-band in the bass).

Reflection problems (comb filtering, early reflections, intelligibility issues) are physically *not fixable by minimum-phase EQ at the listening position* (Toole JAES 2015 p. 517): "These are non-minimum-phase phenomena that are not correctable by minimum-phase equalization." JTS should respond to subjective complaints that look like reflections ("ringy," "thin in the mids," "smeared") by recommending speaker placement changes or acoustic treatment in its audit report, not by adding EQ filters.

### 7. Reversible preference profile — schema patterns

Roon, Sonarworks, WiiM, and Dirac all store EQ profiles as either (a) flat lists of biquad parameter tuples (Roon Parametric EQ, WiiM PEQ, miniDSP biquads), (b) target-curve points (Dirac `.targetcurve` text files of `freq, dB, phase` rows), or (c) semantic objects (Sonarworks "Output Preset" with target mode + Custom Target curve points + Translation Check label). JTS should adopt option (c) — semantic, versioned, reversible — because it can deterministically regenerate (a) or (b) but not vice versa. JSON schemas below.

### 8. Existing tools — see Prior-art comparison table below.

### 9. Safe bounds for preference EQ

Synthesising Roon Headroom Management guidance, Dirac Live practice, and observed safe-defaults from CamillaDSP/REW workflows:

| Bound | Value | Rationale |
|---|---|---|
| Max positive shelf gain | +6 dB | Olive/Welti class-2 boundary; Sonarworks Reference 4 ships a "+6 dB Bass Boost" slider at this limit |
| Max negative shelf gain | −9 dB | Reductive EQ is safe; larger cuts seldom audibly useful |
| Max peak filter boost | +3 dB | Boosting narrow peaks chases phase; Audio Science Review consensus |
| Max peak filter cut | −12 dB | Conservative; deeper cuts often hit measurement noise |
| Max tilt | ±3 dB/octave from 100 Hz–10 kHz | Sonarworks Tilt presets cap at 6 dB total (~0.3 dB/oct over 4 octaves); ±3 dB/oct is roughly 3× the Olive mean and is the practical taste-limit before "wrong" sets in |
| Q range | 0.3–4.0 | Below 0.3 is shelf-like; above 4 risks ringing |
| Auto-reserved digital headroom | max positive gain across full chain + 1 dB | Roon convention; prevents inter-sample clipping |
| Loudness limiter cap | dynamic, scaled to driver excursion model | Use measured/spec'd Xmax-vs-frequency at reference SPL; output gain auto-reduces above per-band SPL ceilings |

Preview/headroom warnings should fire whenever (sum of positive shelf gain + max peak boost) > 6 dB; UX should default to recommending +3 dB shelf + 1 dB peak before warning. Driver excursion considerations: for a small smart-speaker driver (typical Xmax 4–6 mm), boosting below 60 Hz by more than +3 dB at reference SPL is risky; JTS should derive a per-band SPL cap from the driver's Thiele-Small parameters and clamp the dynamic loudness module accordingly.

### 10. A/B comparison and rollback patterns

- **Toggle convention.** Sonarworks SoundID Reference exposes a single "Calibration On/Off" button; per Sonarworks Support beta notes (2023), the current product is engineered for instant toggling: "If you're switching the calibration toggle in the app between enabled/disabled, there should not be any volume jumps" (a notable change from the older Reference 4, which exhibited a ~5 s mute on switching). Roon shows DSP-on/off and signal-path arrows; Dirac uses an "A vs B" with named slots; Audyssey toggles between Audyssey/Flat/L+R Bypass/Off via remote. JTS should expose three radio-button compare modes: **Bypass** (no DSP), **Target Only** (room correction + target, no preference), **Full Chain** (everything). This maps cleanly to the three layers.
- **A/B latency.** JTS should achieve near-instant crossfade by keeping all three chains active in parallel and switching the CamillaDSP Mixer routing at chunk boundaries (a few tens of ms), not by rebuilding/reloading the YAML.
- **Rollback.** Roon Headroom Management retains no real history (single undo); Dirac retains named filter slots; Sonarworks saves a "preset" each calibration. JTS should keep a versioned profile history (last N=20 changes, with `author` ∈ {"user","assistant","wizard"}) and a one-tap "revert to last known good" tied to the design-audit pass.
- **Versioning.** Adopt semver-like: `profile_id`, `schema_version`, `revision`, `parent_revision`, `created_at`, `author`, `change_summary`. Each preference change is a diff against parent; rollback = check out a parent.

---

## Recommended DSP layer model for JTS

```
   ┌─────────────────┐
   │  Audio source   │
   │ (Spotify/HTTP)  │
   └────────┬────────┘
            │ float32 stereo, 48 kHz
            ▼
   ┌─────────────────────────────────────────────────┐
   │ LAYER 0 — Input gain / headroom reserve         │
   │   - one Gain filter, value = −(Σ positive       │
   │     gain in chain + 1 dB)                       │
   │   - tagged source="headroom"                    │
   └────────┬────────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────────────────┐
   │ LAYER 1 — Room/speaker correction               │
   │   - active 20–500 Hz, soft taper to 1 kHz       │
   │   - PEQ biquads from measurement (REW / wizard) │
   │   - bounded: max +3 / −6 dB, Q ≤ 8 only for     │
   │     narrow modes after consensus across mic     │
   │     positions                                   │
   │   - tagged source="room"                        │
   │   - DESIGN AUDIT writes provenance + Schroeder  │
   │     frequency + max-gain summary                │
   └────────┬────────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────────────────┐
   │ LAYER 2 — Target / house curve                  │
   │   - 1 low shelf  (default 105 Hz, +3 dB, Q=0.7) │
   │   - 1 tilt block (default −0.7 dB/oct, 100 Hz – │
   │     10 kHz, implemented as low-shelf + high-    │
   │     shelf pair or FIR)                          │
   │   - 1 high shelf (default 0 dB)                 │
   │   - presets: Neutral / Harman-bass / Bass-lite / │
   │     B&K-1974 / Flat                             │
   │   - tagged source="target"                      │
   └────────┬────────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────────────────┐
   │ LAYER 3 — Preference EQ (reversible)            │
   │   - up to 6 PEQ + 2 shelves, hard-bounded       │
   │     (±6 dB shelf, ±3 dB peak, Q 0.3–4)          │
   │   - human-readable JSON, versioned, diffable    │
   │   - AI assistant can propose; deterministic     │
   │     compiler clamps + emits CamillaDSP YAML     │
   │   - tagged source="preference"                  │
   └────────┬────────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────────────────┐
   │ LAYER 4 — Loudness compensation (optional)      │
   │   - ISO 226:2003 contour, scaled by             │
   │     (replay SPL − reference SPL)                │
   │   - capped per driver excursion model           │
   │   - tagged source="loudness"                    │
   └────────┬────────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────────────────┐
   │ LAYER 5 — Driver protection                     │
   │   - linkwitz HP at driver F3, optional limiter  │
   │   - tagged source="protection" (read-only)      │
   └────────┬────────────────────────────────────────┘
            ▼
       DAC → amp → driver
```

**A/B compare modes.** Mixer-level toggles let UI present: (1) Bypass = skip layers 1–4; (2) Target Only = skip layer 3 + 4; (3) Full Chain; (4) Layer-N bypass (debug).

---

## Recommended target curve schema (JSON)

```json
{
  "schema": "jts.target_curve",
  "schema_version": "1.0.0",
  "id": "target-2026-05-default",
  "name": "JTS Neutral (Harman-anchored)",
  "description": "Default in-room target derived from Olive/Welti AES 8994 (2013), slope from Olive Acoustics Today (2022).",
  "source": "harman_olive_2013",
  "bass_shelf": {
    "type": "low_shelf",
    "freq_hz": 105,
    "gain_db": 3.0,
    "q": 0.7,
    "bounds": { "freq_hz": [40, 200], "gain_db": [0.0, 9.0], "q": [0.5, 1.0] }
  },
  "tilt": {
    "type": "spectral_tilt",
    "implementation": "low_shelf_plus_high_shelf",
    "anchor_low_hz": 100,
    "anchor_high_hz": 10000,
    "slope_db_per_octave": -0.7,
    "bounds": { "slope_db_per_octave": [-1.5, 0.5] }
  },
  "treble_shelf": {
    "type": "high_shelf",
    "freq_hz": 5000,
    "gain_db": 0.0,
    "q": 0.7,
    "bounds": { "freq_hz": [2000, 10000], "gain_db": [-6.0, 3.0], "q": [0.5, 1.0] }
  },
  "correction_range_hz": [20, 500],
  "correction_taper_to_hz": 1000,
  "loudness_compensation": {
    "enabled": false,
    "reference_spl_db": 78,
    "contour": "iso226_2003",
    "max_bass_lift_db": 6.0,
    "max_treble_lift_db": 3.0
  },
  "presets": [
    {"id": "neutral",      "bass_shelf.gain_db":  3.0, "tilt.slope_db_per_octave": -0.7},
    {"id": "bass_lite",    "bass_shelf.gain_db":  0.0, "tilt.slope_db_per_octave": -0.5},
    {"id": "more_bass",    "bass_shelf.gain_db":  6.0, "tilt.slope_db_per_octave": -0.7},
    {"id": "bk_1974",      "bass_shelf.gain_db":  3.0, "tilt.slope_db_per_octave": -0.9, "treble_shelf.gain_db": -3.0, "treble_shelf.freq_hz": 5000},
    {"id": "flat_in_room", "bass_shelf.gain_db":  0.0, "tilt.slope_db_per_octave":  0.0}
  ]
}
```

---

## Recommended preference EQ schema (JSON)

```json
{
  "schema": "jts.preference_profile",
  "schema_version": "1.0.0",
  "id": "pref-2026-05-26T14:02Z-abc123",
  "parent_id": "pref-2026-05-26T13:55Z-xyz789",
  "created_at": "2026-05-26T14:02:14Z",
  "author": "assistant",
  "author_prompt": "more punch and a touch warmer",
  "change_summary": "+2 dB low shelf at 80 Hz, +1 dB low-mid at 200 Hz, mild HF tilt",
  "filters": [
    {"role": "punch",  "type": "low_shelf",  "freq_hz": 80,   "gain_db":  2.0, "q": 0.7, "source": "preference"},
    {"role": "warmth", "type": "peaking",    "freq_hz": 200,  "gain_db":  1.0, "q": 1.0, "source": "preference"},
    {"role": "tilt",   "type": "high_shelf", "freq_hz": 6000, "gain_db": -1.0, "q": 0.7, "source": "preference"}
  ],
  "bounds_check": {
    "max_positive_shelf_db": 6.0,
    "max_positive_peak_db":  3.0,
    "max_total_positive_db_full_chain": 9.0,
    "passed": true
  },
  "headroom_reservation_db": -3.0,
  "applies_on_top_of": {
    "target_curve_id": "target-2026-05-default",
    "room_correction_id": "room-2026-05-19T20:11Z"
  },
  "ab_test_against": "bypass"
}
```

Compilation rule: the deterministic compiler validates against `bounds_check`, sets headroom reservation, then emits CamillaDSP YAML with `description` fields containing the role/source tags so a human reading the YAML can trace every biquad back to a user/AI intent.

---

## Mapping table: user phrase → EQ intent → safe bounds → caveats

References: Pedersen & Zacharov 2015 (AES 9310 "Sound Wheel for Reproduced Sound"); Toole *Sound Reproduction* Ch. 19 vocabulary; Stereophile audio glossary; Olive blog vocabulary commentary; standard mixing-engineer EQ ranges (Apos audiophile glossary; pro audio EQ charts).

| User phrase | Likely intent (band / action) | Default filter | Safe bounds | Caveats |
|---|---|---|---|---|
| "more bass" | Low shelf below ~100 Hz | low_shelf 80 Hz, +3 dB, Q 0.7 | shelf gain ≤ +6 dB; below 60 Hz cap +3 dB (excursion) | Olive class-2 listeners want +4–6 dB; protect driver excursion |
| "less boomy" / "boomy" | Cut at 80–250 Hz (room mode or upper-bass build-up) | peaking 125 Hz, −3 dB, Q 1.5 | peak cut ≤ −12 dB; require measurement to confirm a mode before aggressive cut | "Boomy" is usually a *room mode*, not preference — first recommend re-running room correction with finer Q in modal region; only if user persists, apply preference cut |
| "warmer" | Lift 100–300 Hz +0.5–2 dB; optionally trim 4–8 kHz | peaking 200 Hz, +1 dB, Q 1.0; high_shelf 6 kHz, −1 dB | peak gain ≤ +3 dB; shelf cut ≤ −3 dB | Pedersen–Zacharov sound wheel: "warm" = body/fullness in lower-mid (~100–400 Hz). Overdone → "muddy" |
| "brighter" | Lift 4–10 kHz | high_shelf 5 kHz, +1.5 dB, Q 0.7 | shelf gain ≤ +3 dB | At >+3 dB risks "harsh"; older listeners genuinely benefit (Olive 2015), younger should be capped |
| "more detail" / "more clarity" | Two small moves: −1 dB peak at 200–400 Hz (un-muddy) and +1 dB at 6–10 kHz (air) | peaking 300 Hz, −1 dB, Q 1.0; high_shelf 8 kHz, +1 dB | each ≤ ±2 dB | "Detail" is multi-causal — masking, distortion, room. EQ helps modestly; warn that subjective "detail" often = lower noise floor / better speaker, not EQ |
| "vocals recessed" / "vocals forward" | Lift 1–4 kHz (presence band) | peaking 2.5 kHz, +1.5 dB, Q 1.0 | peak gain ≤ +3 dB | A small target-curve tilt change (less HF roll-off) often does this more naturally than a presence peak; offer both. Risk: sibilance at >+2 dB |
| "harsh" | Cut 2–6 kHz (or 6–10 kHz if "tizzy"), tame "presence/sibilance" band | peaking 4 kHz, −2 dB, Q 1.5 | peak cut ≤ −4 dB | Could also be a speaker resonance — flag if user complaint persists across content; recommend speaker check |
| "thin" | Lift 80–250 Hz; possibly small lift below 60 Hz | low_shelf 120 Hz, +2 dB, Q 0.7; peaking 200 Hz, +1 dB | shelf gain ≤ +4 dB | "Thin" is often near-field placement issue or undersized driver; offer target-curve "more_bass" preset first |
| "muddy" | Cut 150–400 Hz | peaking 250 Hz, −2 dB, Q 1.0 | peak cut ≤ −4 dB | Counterpart of "warmer" — same band, opposite direction. Common cause: boundary loading (speaker too close to wall) — flag in audit |
| "more punch" / "punchy bass" | Lift 60–120 Hz with moderate Q | peaking 80 Hz, +2 dB, Q 1.2; or low_shelf 100 Hz +2 dB | peak gain ≤ +3 dB; shelf ≤ +4 dB | "Punch" = bass *transient* + upper-bass energy. EQ can help upper-bass; transient response is driver/cabinet, not EQ. Cap to protect excursion |
| (bonus) "bigger soundstage" | Likely a complaint EQ cannot solve — recommend toe-in/placement | — | — | Stereophile/Toole: imaging is dominated by speaker placement, room symmetry, early reflections, not EQ |
| (bonus) "tinny" | Lift 80–250 Hz, cut 2–4 kHz | low_shelf 120 Hz +3 dB; peaking 3 kHz −2 dB | shelf ≤ +4 dB; peak ≤ −4 dB | Often a band-limited speaker or laptop-output context |

**Important rule for the assistant:** any phrase that maps to a *cut* below the room's transition frequency triggers a re-measurement suggestion before authoring preference filters; any phrase that maps to a *boost* causes the headroom auto-reservation to be re-computed and a "headroom now reserved at −X dB" notification.

---

## UX recommendations

### Novice mode
- Three sliders only: **Bass** (−6 to +6 dB, default 0, maps to target bass shelf gain), **Tilt / Treble** (−3 to +3 dB, default 0, maps to target tilt slope), **Loudness** (Off / Auto, off by default).
- One toggle: **Room Correction** (On/Off).
- One A/B compare button that cycles Bypass → Target Only → Full Chain.
- Hide all biquad parameters. Hide preference EQ entirely; expose only via the AI assistant chat ("ask for more bass, warmer, etc.").
- Show a single "design-audit health" dot (green/amber/red) summarising correction headroom, max boost, Schroeder-frequency compliance.

### Power-user mode
- Full read/write of `target_curve` and `preference_profile` JSON.
- Live REW import / export (REW PEQ format `freq, gain, Q`); CamillaDSP YAML import/export with provenance tags preserved.
- Per-layer bypass; chain diagram view; per-band SPL ceiling overlay; headroom waterfall.
- Profile history with diff view and one-click revert.
- Loudness compensation manual: reference SPL, ISO 226 curves, max-lift caps.

---

## Prior-art comparison table

| Tool | Target curve customization | Preference layer (separate from target) | A/B comparison | Profile management | Integration with room correction |
|---|---|---|---|---|---|
| **Dirac Live** | Drag-handle UI on top of legacy −0.5 dB/oct default *or* 2022 measurement-adaptive Auto Target Curve; import `.targetcurve` text files | None as a distinct layer — preference folded into target edits | Slot-based A/B; full bypass | Multiple filter slots in AVR; export/import via PC | Tight — correction is computed to match target |
| **Sonarworks SoundID Reference** | Flat / Dolby / B&K 1974 / X-Curve / Tilt presets + Custom Target parametric EQ | "Custom Target" is the preference layer (sits on top of Flat) | Calibration On/Off (now engineered for near-instant toggling per Sonarworks Support, 2023), plus "Translation Check" simulations | Output Presets (named); per-input chain; cloud sync | Correction → target → custom target → translation, all chained |
| **Genelec GLM 5** | Implicit anechoic-flat target; Sound Character Profiler for global shape | Sound Character Profiler is the explicit voicing layer | Per-group bypass; multi-preset switching | Up to 30 monitors, multiple group/preset definitions | AutoCal (room) and Sound Character (voice) are explicitly separate UI tabs |
| **Neumann MA 1** | Room-adaptive auto target + manual target curve editor; Fraunhofer IIS algorithm | Manual target edits = de facto preference layer | Switch between alignment presets (named) | Multiple alignment presets per setup | Stored in monitor DSP; no plugin/driver needed |
| **Audyssey MultEQ** | Reference (auto HF roll-off curve) / Flat / L+R Bypass | Dynamic EQ = loudness compensation referenced to MultEQ XT calibration with offsets 0/5/10/15 dB (per Denon/Marantz AVR documentation); not a free-form preference EQ | Audyssey on/off; mode select | Per-input modes in AVR; MultEQ Editor App for advanced edits | Single full-range filter; no clean layer split |
| **Roon (MUSE DSP)** | Parametric EQ shelves manual; Procedural EQ for per-channel | Same Parametric EQ; convention is "Convolution = room, PEQ = preference" but UI doesn't enforce | Enable/disable per filter; chain on/off | DSP presets per zone; manual export | Convolution slot for FIR room filter; Headroom Mgmt explicit |
| **WiiM Home + RoomFit** | Flat / B&K / Harman selectable; up to 10 bands of PEQ per source (current firmware) | Since the Aug 2025 firmware split, RoomFit (10 bands) and PEQ (10 bands) live in separate layers per WiiM forum | Preset switching; EQ on/off | Multiple EQ presets per device | RoomFit + manual PEQ now distinct |
| **HouseCurve (iOS)** | Choose target (Harman/B&K/flat/custom) | None — generates PEQ targeting selected curve | Manual A/B by toggling EQ on destination device | Save measurements; export PEQ text | Target + measured → PEQ filters; relies on destination's EQ slots |
| **miniDSP (Dirac variants & manual)** | Dirac Live target (above) + raw biquad slots | Manual biquad slots = preference | Per-config A/B | Config slots in plugin | Dirac Live filter loaded into device |
| **AutoEQ** | Harman / custom target; per-headphone targets | None — single filter chain | None native | Plain text/CSV/YAML | None (headphone-only) |
| **JamesDSP / Equalizer APO + PEACE** | Manual PEQ; importable target curves | Combined | Profile switching | Profile files | None unless user-authored |
| **CamillaDSP (raw)** | None native | None native | None native | YAML files, user-managed | None native |
| **JTS (proposed)** | Target schema with bass shelf + tilt + treble shelf + presets | Separate `preference_profile` JSON with hard bounds; AI-assistant friendly | 3-way: Bypass / Target Only / Full Chain | Versioned, parent-linked, author-tagged (user/assistant/wizard) | Room correction restricted to 20–500 Hz, soft taper, tagged in chain |

---

## Recommendations (staged, with thresholds)

**Stage 1 — Ship the architecture (now).** Implement the six-layer pipeline with provenance tags. Restrict room correction's active range to 20–500 Hz hard, 500–1000 Hz soft taper, identity above. Default target = Harman-anchored neutral (low shelf +3 dB / 105 Hz / Q 0.7, tilt −0.7 dB/oct, treble shelf 0 dB). Implement bounded preference EQ with auto-headroom. **Trigger to change:** if user testing shows confusion about which layer is responsible for which artefact, add an explicit "What's doing what?" breakdown in the audit report.

**Stage 2 — Three preset target families (next minor).** "Neutral" (default), "More Bass" (+6 dB shelf, for the ~15% class-2 listeners), "Bass-Lite" (0 dB shelf, slightly less tilt, for the ~21% class-3 listeners), plus "B&K 1974" and "Flat" for power users. **Trigger to add more:** if opt-in telemetry shows ≥ 10% of users gravitating to a custom curve cluster not covered by the presets, codify it.

**Stage 3 — Loudness compensation (after baseline ships).** Implement ISO 226:2003 dynamic loudness with reference 78 dB SPL, caps +6 dB bass / +3 dB treble at 50 dB replay. Off by default. **Trigger to enable by default:** never; this is a per-user preference like Audyssey Dynamic EQ.

**Stage 4 — AI assistant integration.** The assistant operates by emitting a structured *intent object* (e.g., `{"intent": "more_punch", "magnitude": "moderate"}`) which the deterministic compiler turns into preference filters using the mapping table above. The LLM never writes biquad coefficients. **Trigger to expand vocabulary:** add new phrases only after they have a published reference (sound-wheel attribute, AES paper, Toole vocabulary chapter) or a documented user-study mapping.

**Stage 5 — Telemetry-driven validation.** Collect (opt-in, aggregated) data on preference-slider final positions, A/B selection rates, rollback frequency. Use to validate that JTS users fall into the same three classes (64/15/21) as Olive's data; if they don't, that's strong signal to tune defaults.

**Hard constraints that should never change without explicit override:**
- Room correction frequency upper bound (Toole 2015 transition frequency rule).
- Maximum positive total chain gain before forced headroom reservation.
- Provenance tagging of every biquad in the chain.
- Versioned, reversible preference profiles.

---

## Caveats and Open Questions

1. **Single-cabinet smart speaker ≠ stereo pair in a treated room.** Most Harman/Toole research used left–right stereo with the listener in the sweet spot. JTS is likely consumed at a variety of off-axis positions; the in-room target curve research applies in spirit but the +6 dB bass figure may be too high for a desktop/near-field use case (Sound on Sound's B&K commentary explicitly warns that near-field listening makes any B&K-style curve sound bright). Recommend an optional "near-field" preset that reduces the tilt to −0.3 dB/oct and bass shelf to +1 dB.

2. **Welti's "+6 dB bass" figure is the inter-listener mean — class structure matters more than the mean.** The class breakdown (64/15/21%) from Olive 2018/2022 is more nuanced than a single mean; JTS should use the *class structure*, not the single mean, to define presets. A formal per-frequency σ is not in the public abstract of AES 8994; reading the full paper from the AES e-library is recommended before publishing user-facing claims that include uncertainty bounds.

3. **Schroeder vs. transition frequency.** Toole explicitly notes that "Schroeder" is the technically correct term for the *large* reverberant-room boundary, while small-room "transition frequency" is a closely related but smaller-room concept ("the Schroeder frequency in large reflective rooms"). The standard formula `2000·√(T60/V)` is, per Toole, valid for large concert halls; for a domestic room the practical transition is empirically ~200–500 Hz. JTS's audit report should label the computed value clearly as an *estimate*.

4. **AI ↔ deterministic interface contract.** Open question: should the assistant return *intent objects* (recommended), *suggested filter parameters*, or *natural language descriptions*? Intent objects are safest (the compiler enforces every bound) but constrain LLM expressiveness. A hybrid (intent + free-text rationale shown to the user) is probably right. Needs prototyping.

5. **Headphone Harman targets are measurement-rig-specific.** Olive has documented that GRAS 45 CA-based targets do not translate to B&K Type 5128 measurements; JTS's UMIK / phone-mic measurement chain is its own measurement domain. If JTS ever extends to headphone calibration, it must re-derive (or licence) a target valid for its own measurement fixture.

6. **Driver protection vs. user preference.** A user asking for "+6 dB bass" on a small driver may be physically incompatible with reference SPL. The system should auto-attenuate by reducing reference SPL and *announce* the trade-off ("max SPL reduced from 92 dB to 88 dB to allow your requested bass") rather than silently failing or clipping.

7. **Cultural/regional preference data is thin.** Olive's "consistent across 4 countries" claim (283 listeners, Canada/US/Germany/China) is based heavily on Harman employees and recruited participants; broader cultural data is sparse. JTS should not assume the curve is universal — keep the preference layer reversible and user-driven.

8. **Reflection problems masquerading as tonal problems.** A user complaint of "harsh" might be early-reflection-induced rather than tonal. Toole's framework says minimum-phase EQ can't fix this; JTS should detect long modal decay or strong reflections in the measurement and recommend room treatment / speaker placement before committing preference EQ.

9. **Linear-phase vs minimum-phase choice in the FIR convolution.** Linear phase preserves transient response (Sonarworks default); minimum phase preserves causality and reduces pre-ringing. CamillaDSP supports both via Conv FIR. Default recommendation: minimum-phase IIR for room correction (audibly preferred for bass per Mäkivirta et al., *JAES* 51(5), 2003), FIR linear-phase optional for power users at higher latency.

10. **Telemetry ethics.** Recommendations in Stage 5 depend on opt-in aggregated data; JTS being open-source-ish, the project should publish the schema of any telemetry it collects and let users opt out without losing functionality.