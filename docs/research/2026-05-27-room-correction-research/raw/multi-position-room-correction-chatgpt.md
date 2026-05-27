# Multi-Position Room-Correction Confidence for JTS

## Executive summary

The strongest finding in the literature is that room correction has to treat low frequencies and mid/high frequencies as different problems. Below the room’s transition or Schroeder region, equalization has a legitimate role because low-frequency resonances behave much more like minimum-phase systems, and spatial averaging can reveal the underlying response that is worth correcting. Above that region, the microphone sees dense constructive and destructive interference from direct and reflected sound; narrow peaks and dips there are often non-minimum-phase, strongly position-dependent, and risky to “fix” with conventional PEQ. Floyd Toole is explicit that single- or few-point equalization above the transition region can degrade a good loudspeaker elsewhere, while low-frequency spatially averaged measurements and modal attenuation are useful. citeturn5view0turn5view2turn5view3turn4view0

For JTS, the best deterministic strategy is therefore not “average everything and EQ it.” It is to maintain, per band, three distinct views of the data: an incoherent energy average for what the room is doing on average, a robust central tendency such as median or smoothed dB-average for what listeners broadly experience, and a spread metric such as percentile span or standard deviation for how seat-dependent the feature is. REW’s own documentation supports this split: RMS averaging converts dB to linear magnitude and treats spatial measurements as incoherent; dB averaging can be useful on smoothed traces for deriving an EQ target; vector averaging is only appropriate when measurements are from the same position or have meaningful time and level alignment. citeturn7view0turn7view1turn7view2turn7view3

The next key finding is psychoacoustic and conservative: peaks are more audible than equivalent dips, and broad or high-Q resonances become more audible as width and ringing increase. Bücklein reported that peaks are easier to hear than equivalent valleys, and Toole and Olive summarized earlier work showing that detectability varies with Q, frequency, signal type, and delay, while also warning that the measurement resolution must be sufficient to reveal resonances without mistaking interference ripples for them. That supports JTS’s current “cuts first, bass/modal range first” stance and strongly argues against automatic boost into narrow notches, SBIR dips, or cancellation nulls. citeturn43view0turn41view1turn41view3

On multi-position practice, the public prior art is surprisingly consistent. Audyssey recommends at least eight positions, says three is the minimum to proceed, and notes diminishing returns beyond roughly eight to ten positions in small rooms. Dirac’s public material refers to nine measurement positions and states that standard room correction aims to meet the target response on average within the measured area. Sonarworks uses a weighted average of 37 points in a small nearfield listening zone. HouseCurve recommends averaging 3–5 positions for a desk and 3–7 for a living room, while warning against using a single measurement for EQ. RoomPerfect explicitly builds up “RoomKnowledge” by adding more room measurements, and Trinnov exposes per-point weights in multi-point calibration. Those systems differ in math and goals, but they all encode the same practical truth: multi-position room correction is fundamentally a measured-area optimization problem, not a single-seat inversion problem. citeturn25view3turn29search1turn31view1turn25view2turn23search7turn25view0turn25view5turn25view6turn26view2

For JTS specifically, I recommend a default workflow of **five positions** for consumer usability, with **three as the minimum for multi-seat correction**, **seven to nine for wide couches or higher confidence**, and **single-position mode only as a limited seat-specific fallback**. Five is not a magic literature number; it is a conservative product recommendation inferred from the public practice above, especially HouseCurve’s 3–7 living-room guidance, Dirac’s nine-point convention, and Audyssey’s diminishing returns beyond about eight to ten in small rooms. The right way to represent that in the UI is not “five is enough for science,” but “five is the default compromise; more positions increase confidence, chiefly by letting the software distinguish stable room features from seat-local artifacts.” citeturn25view0turn25view3turn31view1

My highest-confidence recommendations are these. **High confidence:** keep automatic correction primarily below about 150–250 Hz unless a feature is broad, stable, and clearly shared across seats; never auto-boost narrow deep dips; do not use vector averaging across arbitrary seats; and surface measurement-quality gates based on clipping, timing reliability, ambient noise, and repeatability. **Medium confidence:** adopt a five-position default, with high/medium/low confidence bands driven by seat-to-seat spread and immediate repeatability thresholds. **Unresolved:** there is no single public, peer-reviewed threshold that directly maps “x dB seat variance” to “safe to correct” across all domestic rooms, so JTS should present those thresholds as conservative engineering gates rather than immutable acoustical law. citeturn5view0turn5view2turn7view0turn7view1turn33view0turn33view2

## What the literature and prior art actually support

A long line of multiple-point equalization research predates consumer room-correction products. The 2009 JASA paper by Lingvall and Brännmark frames the central problem well: the goal is a correction filter that is robust to listener movement within a predefined region of interest, using responses measured at several positions and accounting for estimation errors. Importantly, their formulation also introduces a time-dependent reflection filter that attenuates the high-frequency portions of reflected responses that a single-source system cannot compensate, which is directly relevant to JTS’s desire to remain conservative above the transition region. The same paper also cites the earlier multiple-point literature by Elliott and Nelson, Haneda et al., and Bharitkar and Kyriakakis. citeturn12view0

Toole’s work provides the most useful perceptual boundary conditions for a JTS-style system. He states that conventional equalization cannot add or remove reflections, change reverberation time, reduce seat-to-seat bass variations, or fix loudspeaker directivity. He also separates the acoustics above and below the transition region: above it, narrow in-room ripples are caused by direct-plus-reflected interference and are not correctable by minimum-phase equalization; below it, low-frequency room resonances can be attenuated, and adjacent-boundary effects in the upper-bass region can be treated using spatially averaged measurements. He further notes that multiple subwoofers can reduce seat-to-seat bass variation, which means EQ should be viewed as the last stage, not the whole solution. citeturn5view0turn5view2turn5view3turn4view0

That “EQ is not geometry control” result is echoed by modern practice in subwoofer optimization. Multi-Sub Optimizer explicitly defines its job as flattening the response, reducing seat-to-seat variation, and maximizing SPL in the modal region, and its documentation centers the modal range on roughly the lowest frequencies up to around 200 Hz in typical home rooms. miniDSP’s MSO application notes describe “minimize seat-to-seat variation” as a core objective, and then hand off overall tonal shaping to a later target-curve stage. That is a strong hint for JTS architecture: if seat variance is high, the correct software behavior is often to **warn about placement or source count**, not to push PEQ harder. citeturn36view0turn35search14turn35search15

The psychoacoustic literature supports a correction policy that privileges broad, repeatable excess energy over isolated deficits. Bücklein’s work, as summarized in later literature, found that peaks are more audible than equivalent valleys and that both become more audible as width increases. Toole and Olive likewise discuss how resonance audibility depends on Q, frequency, program material, delay, and other factors, and they warn that insufficient frequency resolution will smooth away high-Q resonances while excessive resolution can over-emphasize fine ripples that are not practically actionable. Their conclusion is not “ignore narrow resonances,” but rather “measure them with enough resolution, then interpret them in context.” citeturn43view0turn41view1turn41view3

At low frequencies, time-domain severity matters a great deal. Mäkivirta and colleagues argue that low-frequency modal equalization is especially relevant because passive absorption becomes expensive and local control becomes easier in the wavelength regime where the sound field is no longer diffuse. Their work targets control below 200 Hz and emphasizes modal identification, decay behavior, and reverberation-time targets rather than simple steady-state flattening. Later perceptual work from Fazenda, Stephenson, and Goldberg measured practical modal-decay thresholds and found that, for musical stimuli, perceptual thresholds decrease from about 0.51 s at 63 Hz to about 0.12 s at 250 Hz; for artificial stimuli the thresholds run from about 0.9 s at 32 Hz down to about 0.17 s at 200 Hz. Separate work by Avis, Fazenda, and Davies found that changes in Q are easier to detect at higher reference Q, with indicative average difference limens near Q≈16.5 at reference Q=1, ≈10.1 at Q=10, and ≈6.0 at Q=30; they also note that these figures are indicative rather than conclusive outside the tested cases. For JTS, this means long-decay low-frequency modes deserve extra confidence weighting even when their steady-state amplitude is not the single biggest outlier. citeturn16view0turn46search2turn46search5turn45view0

The practitioner/open-source tooling mostly agrees with the science above. REW explicitly distinguishes RMS, dB, and vector averaging, and it warns that vector averaging across unaligned or spatially different measurements can create magnitude dips from phase cancellation. HouseCurve warns that a single location should not be used for equalization and recommends averaging multiple locations over the listening area. Jean-Luc Ohl’s moving-microphone method is explicitly framed as a spatial-averaging method intended for in-room EQ, not for loudspeaker R&D or time-alignment work, and cites good repeatability when the purpose is deriving a zone average rather than preserving pointwise detail. Carl Hopkins’s JASA work on manual scanning paths, although aimed at room-average SPL measurement, shows that scanning paths can efficiently generate many effectively uncorrelated samples above 200 Hz. The implication for JTS is straightforward: fixed-position measurements are better for **confidence reporting**, while MMM is valuable as a **fast average estimator** or future adjunct, because MMM intentionally suppresses the very spatial variance information that JTS now wants to expose. citeturn7view0turn7view1turn25view0turn20view0turn22view0

## Practical recommendations for JTS

JTS should treat **stereo** as the primary target and **mono** as a first-class fallback, but the measurement logic should be channel-centric in both cases. For stereo, measure left and right separately at each position and build correction candidates per channel from the single-channel responses, not from an always-on L+R summed response. REW’s own documentation notes that some resonances visible in an individual speaker response may not appear when left and right are driven together, because room-mode excitation changes with source symmetry; that is useful diagnostically, but it is a poor basis for designing per-channel correction filters. If JTS later wants a “what the user hears in stereo” preview, it can show a separate L+R summary trace, but audible-seat confidence and filter decisions should still be anchored in the per-channel data. citeturn33view0

For the default consumer experience, I recommend a **five-position cluster** around the main listening point. In a single-seat setup, those five points should occupy roughly a head-sized region rather than the whole room: center, left, right, forward, and back around ear height, with slight vertical variation between some measurements. Audyssey explicitly recommends moving the mic around the center of the listening area, avoiding room extremes, and varying height by a few inches between positions; HouseCurve’s guidance for living rooms is 3–7 positions in the listening area. JTS can therefore present five positions as a “balanced default,” with seven or nine positions offered as “higher confidence for wider seating areas.” citeturn25view3turn25view0

If a user provides a richer point map, JTS should not automatically become more aggressive. It should become **more certain** and more diagnostic. With 7–9 fixed points, Trinnov-style point weighting becomes practical, percentile bands become more stable, and seat-local features become easier to reject. With still denser maps, JTS could estimate bandwise spatial surfaces, identify where modal problems persist across a couch, and separate “stable room excess” from “one-seat null” with greater confidence. That is especially useful for future FIR or multi-sub work, but the immediate win is better **confidence reporting**, not necessarily more filters. Audyssey’s documentation explicitly says that more than roughly eight to ten positions in a small room rarely improves the calibration much, which reinforces the idea that richer sampling mainly improves certainty and diagnostics once you already have a decent measured-area estimate. citeturn26view2turn25view3

For averaging, JTS should not choose a single canonical average. It should compute at least four deterministic summaries for every band: a lightly smoothed **RMS/linear-power average**, a lightly or moderately smoothed **median or dB-average**, **percentile spread** such as P10/P90 or min/max after outlier control, and **repeatability** from repeated measurements. The RMS average is the best main workhorse for low-frequency correction candidate detection because it preserves the weight of excess acoustic energy; the median or smoothed dB-average is better for perceptual trend displays and target-shape comparison; spread and repeatability tell you whether a feature is stable enough to correct. REW’s own definitions map cleanly onto this scheme. citeturn7view0turn7view1turn7view2

For frequency range, JTS should keep auto-generated PEQ mostly in the **bass and lower transition region**. A good first release target is an auto-correction ceiling around **150–200 Hz for “safe,” 200–250 Hz for “balanced,” and only up to about 300 Hz for “assertive,”** with aggressive safeguards above 150–200 Hz. That recommendation is an inference from Toole’s transition-region cautions, Toole’s statement that adjacent-boundary effects are likely below about 300 Hz, Mäkivirta’s focus below 200 Hz, and MSO’s emphasis on the modal region up to around 200 Hz in typical rooms. It fits JTS’s stated philosophy and stays within what the literature supports best. citeturn5view0turn5view3turn16view0turn36view0

For single-position fallback, JTS can still say a few important things with confidence, but only a few. It can judge **measurement integrity** very well: clipping, noise contamination, timing problems, and corrupt impulse responses. It can also identify **broad, obvious low-frequency excess** at that seat and, in some cases, safely attenuate it for that seat only. It can identify **likely cancellations or nulls** and refuse boosts. What it cannot do from a single seat is claim that a feature is spatially stable or representative of the listening area. That is exactly why HouseCurve advises against using a single measurement for EQ, while Toole still allows low-bass attenuation of prominent resonances at a single listening position as a practical last touch. JTS should therefore expose single-position results as **seat-specific confidence**, not **room-area confidence**. citeturn25view0turn5view2

## Proposed confidence schema and deterministic algorithms

A useful JTS confidence model should be **band-centric**, **filter-centric**, and **bundle-replayable**. At the band level, it should answer four questions: is the data reliable, is the feature spatially stable, is the feature physically plausible for EQ, and is the proposed correction conservative enough? At the filter level, it should answer why this filter exists, why it is cut-only or limited-gain, what seats support it, what seats disagree, and what specific warning gates were applied. This is exactly the kind of explanation that later makes an AI or LLM assistant safe, because the narrative can be generated from deterministic facts rather than from hidden heuristics. The public products that come closest to this spirit are RoomPerfect’s “RoomKnowledge,” Trinnov’s per-point weighting, and MSO’s explicit optimization targets, but none of them exposes the conservative per-band rationale that JTS wants; that is an opportunity for the project. citeturn25view5turn25view6turn26view2turn36view0

I recommend giving each band and each candidate filter five deterministic scores:

| Field | Meaning | Why it matters |
|---|---|---|
| `data_quality` | Measurement integrity: clipping, noise margin, timing integrity, repeatability | Prevents confidence inflation from bad data |
| `spatial_stability` | How similar the feature is across seats: spread, sign consistency, percentile span | Separates stable room behavior from seat-local artifacts |
| `eq_plausibility` | Whether the feature looks minimum-phase and broad enough to correct | Rejects likely combing, nulls, SBIR notches, and ultra-narrow ripples |
| `benefit_estimate` | Expected usefulness of a conservative cut based on prominence, bandwidth, and decay severity | Prioritizes broad modal excess and ringing |
| `risk` | Boost risk, high-Q risk, out-of-band risk, channel mismatch, placement suspicion | Lets JTS gate safe/balanced/assertive modes |

That structure follows directly from the literature’s separation of measurement quality, spatial robustness, and low-frequency modal audibility. citeturn5view0turn12view0turn16view0turn46search2

A practical **confidence-grade schema** for JTS would look like this:

| Grade | Meaning | Recommended action |
|---|---|---|
| **High** | Reliable measurement, low seat spread, broad excess energy, repeatable, within conservative correction band | Allow normal cut-only PEQ |
| **Medium** | One metric is marginal: fewer points, moderate spread, weaker repeatability, or broader placement ambiguity | Allow smaller cuts and stronger warnings |
| **Low** | Poor data quality, high spread, narrow/notch-like feature, sign flips, likely cancellation, or out-of-band | Do not auto-correct; warn or ask for re-measure |

The strength of this scheme is that it gives JTS a natural mapping to strategy presets. “Safe” can require all filters to be high confidence. “Balanced” can admit some medium-confidence cuts. “Assertive” can widen the frequency range and filter limits only when the measurement bundle itself is exceptionally good. citeturn25view3turn29search1turn26view2

Here is pseudocode for a conservative first implementation:

```python
def analyze_bundle(bundle):
    qc = evaluate_measurement_quality(bundle)
    # qc includes clipping flags, timing integrity, ambient/noise checks,
    # headroom, and immediate repeatability where available

    results = {}

    for ch in bundle.channels:  # mono -> one channel, stereo -> L and R
        responses = load_responses(ch, bundle)

        # Use magnitude-domain summaries for spatial averaging
        rms_avg = rms_average_mag(responses)          # incoherent energy average
        med_db  = median_db(responses)               # robust central tendency
        p10_db, p90_db = percentile_band(responses)  # spread
        rep_db  = repeatability_metric(responses)    # immediate repeats if available

        candidates = find_broad_features(
            curve=med_db,
            limit_hz=bundle.strategy.f_corr_max_hz,
            min_prominence_db=bundle.strategy.min_prominence_db
        )

        accepted = []
        rejected = []

        for feat in candidates:
            width_ok = feat.bandwidth_oct >= min_width_for(feat.center_hz)
            spread_db = p90_db[feat.idx] - p10_db[feat.idx]
            sign_consistency = fraction_of_positions_with_same_sign(responses, feat)
            repeatable = rep_db[feat.band] <= repeat_limit_for(feat.center_hz)

            null_like = (
                feat.is_dip or
                feat.narrow_and_deep or
                spread_db > bundle.strategy.max_spread_db or
                sign_consistency < bundle.strategy.min_sign_consistency
            )

            placement_like = broad_feature_shared_across_all_positions(feat) and \
                             feat.center_hz >= bundle.strategy.placement_warning_hz

            if qc.bad or null_like:
                rejected.append(reason(feat, "likely null/cancellation or poor data"))
                continue

            if not width_ok:
                rejected.append(reason(feat, "too narrow / seat-local"))
                continue

            if sign_consistency >= 0.8 and spread_db <= 4.0 and repeatable:
                conf = "high"
            elif sign_consistency >= 0.6 and spread_db <= 6.0:
                conf = "medium"
            else:
                rejected.append(reason(feat, "insufficient spatial stability"))
                continue

            filt = design_cut_only_peq(
                center_hz=feat.center_hz,
                gain_db=bounded_cut(feat.prominence_db, conf),
                q=max_q_for(feat.center_hz, bundle.strategy)
            )

            accepted.append({
                "filter": filt,
                "confidence": conf,
                "placement_warning": placement_like,
                "evidence": {
                    "spread_db": spread_db,
                    "sign_consistency": sign_consistency,
                    "repeatability_db": rep_db[feat.band],
                    "bandwidth_oct": feat.bandwidth_oct,
                }
            })

        results[ch] = {
            "qc": qc,
            "accepted_filters": accepted,
            "rejected_features": rejected,
            "summaries": {
                "rms_avg": rms_avg,
                "median_db": med_db,
                "p10_db": p10_db,
                "p90_db": p90_db,
            }
        }

    return results
```

This algorithm is intentionally stricter than most consumer room-correction systems, but its structure directly reflects the public evidence: spatially averaged low-frequency correction is useful, high-frequency interference is risky, vector averaging must be reserved for phase-meaningful aligned data, and measurement integrity must be explicitly gated. citeturn5view0turn5view2turn7view0turn7view1turn33view0turn33view2

Two implementation details are especially important. First, when JTS stores complex responses, it should use them mainly for **alignment and diagnostics**, not for spatial correction averaging. REW is explicit that vector averaging is most appropriate for repeated same-position measurements or time/level aligned data, and that RMS+dB-plus-phase averages can create acausal or otherwise unnatural relationships. Second, JTS should compute an explicit **null likelihood** rather than merely saying “dips are bad.” A likely null is a feature that is deep, narrow, unstable across seats, or changes sign between seats. That is the deterministic bridge between Toole’s non-minimum-phase warning and JTS’s no-boost policy. citeturn7view1turn7view3turn5view0turn43view0

## Suggested thresholds and decision gates

The table below separates what I regard as **high-confidence thresholds** from **medium-confidence engineering defaults**. The high-confidence items are directly anchored in literature or official tool behavior. The medium-confidence items are conservative design recommendations inferred from the evidence and from current room-correction practice.

| Item | Recommendation for JTS | Confidence | Rationale |
|---|---|---|---|
| Minimum positions for multi-seat correction | **3** minimum | **High** | Audyssey requires at least 3 to proceed; fewer than that cannot support variance reporting with much credibility. citeturn25view3 |
| Default positions | **5** | **Medium** | Best product compromise between burden and statistics; consistent with HouseCurve’s 3–7 living-room guidance and below Audyssey’s diminishing-return region. citeturn25view0turn25view3 |
| Higher-confidence mode | **7–9** positions | **Medium** | Aligns with Dirac’s public nine-point flow and improves percentile stability on wider seating. citeturn31view1turn29search1 |
| Single-position fallback | Allow only **seat-specific** analysis and minimal LF cuts | **High** | HouseCurve warns against single-point EQ; Toole allows LF attenuation of prominent resonances at a single location. citeturn25view0turn5view2 |
| Primary correction region | **20–150/200 Hz** | **High** | Low-frequency resonances are the strongest case for EQ; Mäkivirta focuses below 200 Hz; MSO centers on the modal region; Toole’s caution rises sharply above transition. citeturn16view0turn36view0turn5view0 |
| Secondary caution region | **200–300 Hz** warnings first, correction only if broad and stable | **High** | Toole identifies adjacent-boundary effects likely below about 300 Hz, but still separates this zone from safer low-bass correction. citeturn5view3turn4view0 |
| Automatic boosts into narrow/null-like dips | **Never** | **High** | Peaks are more audible than dips; interference dips are often non-minimum-phase and not correctable by conventional EQ. citeturn43view0turn5view0 |
| Filter polarity policy | **Cuts first; boosts off by default** | **High** | Strongly supported by the same evidence above and by the lack of spatial robustness of dips. citeturn43view0turn5view0 |
| Measurement clipping gate | Reject or re-measure if clipping occurs; warn if headroom < **6 dB** | **High** | REW flags <6 dB headroom as close to clipping and warns that clipping corrupts derived response. citeturn33view0turn33view2 |
| Timing gate with external/USB mic | Use acoustic timing reference; avoid multi-sweep pre-averaging on separate I/O devices | **High** | REW warns of clock-rate distortions and corruption when input/output devices differ. citeturn33view1turn33view4 |
| Ambient noise gate | Room must be quiet enough for stable repeats; otherwise ask for re-measure | **High** | Audyssey and Trinnov both insist on quiet rooms; REW ties higher S/N to more accurate IR/FR estimates. citeturn25view3turn26view0turn33view1 |
| Seat-spread threshold for high confidence | Standard deviation ≲ **2 dB** or P90–P10 span ≲ **4 dB** in the candidate band | **Medium** | Conservative engineering threshold intended to keep auto-EQ to clearly shared features; consistent with the idea that ±3 dB is already generous for broad in-room tolerances. citeturn5view4turn20view0 |
| Seat-spread threshold for medium confidence | Standard deviation about **2–4 dB** or P90–P10 span about **4–8 dB** | **Medium** | Above this, the feature increasingly looks seat-dependent rather than global. citeturn20view0turn25view0 |
| Minimum peak width | At least about **1/6 octave** below 100 Hz; about **1/3 octave** above 100 Hz | **Medium** | Bücklein and Toole/Olive show audibility rises with width, while Toole warns against chasing fine ripples above the transition region. citeturn43view0turn41view3 |
| Maximum Q for auto-PEQ | **Q ≤ 6** default, hard ceiling **8** below deep bass; much lower above transition | **Medium** | Conservative proxy for “broad enough to help more than one seat.” This is an inference, not a published universal optimum. Supported by the width logic above. citeturn43view0turn41view3 |
| Maximum cut gain | **4 dB safe**, **6 dB balanced**, **8 dB assertive** | **Medium** | Large cuts usually indicate geometry/source-placement issues. JTS should keep PEQ modest unless the feature is extremely stable and broad. citeturn5view0turn36view0 |
| Repeatability threshold | Immediate repeat within about **1 dB RMS** in 20–200 Hz for high confidence; about **2 dB RMS** for medium | **Medium** | Conservative engineering gate supported by REW’s emphasis on S/N and by MSO’s insistence that unreliable measurements destroy prediction accuracy. citeturn33view1turn36view1 |

Two more low-frequency thresholds are particularly useful for a confidence report. First, if JTS can estimate modal decay from the deconvolved impulse response, it should flag bands whose decay is above the **practical music-threshold range** reported by Fazenda et al.; roughly speaking, the threshold falls from about 0.5 s in the 63 Hz region toward about 0.12 s by 250 Hz for music-like stimuli. Second, if JTS estimates or fits modal Q, it can use the Avis/Fazenda/Davies results as an **interpretive aid**: higher-Q resonances need smaller Q changes to become perceptibly different, so very high-Q narrow bass features deserve extra skepticism when they are spatially unstable. Those are best used to prioritize warnings and decay displays, not as hard gate values for filter design. citeturn46search2turn45view0

A good strategy gate for JTS would look like this in plain language. **Safe:** at least three positions, no clipping/timing failures, only high-confidence cuts below 200 Hz, Q capped tightly, no boosts. **Balanced:** preferably five positions, one or more medium-confidence features allowed if they are broad and repeatable, ceiling around 250 Hz. **Assertive:** at least seven positions, strong repeatability, and only then allow somewhat wider correction range or larger cuts; still no boosts into likely cancellations. This architecture is stricter than most market products, but it is much closer to what the literature actually supports. citeturn25view3turn31view1turn29search1turn46search7

## Prior-art comparison table

The table below separates **confirmed public technical details** from **inference or marketing that should not be treated as fact**.

| Tool or workflow | Confirmed public technical details | Inference or marketing caveat | What JTS should borrow |
|---|---|---|---|
| **REW** | Distinguishes RMS average, dB average, vector average, and RMS/dB+phase averages; says vector averaging is for same-position or aligned measurements; provides clipping/headroom/timing-reference guidance. citeturn7view0turn7view1turn33view0turn33view1 | REW does not ship a “confidence engine”; you have to build one around its measurement primitives. | Use its averaging taxonomy almost verbatim in JTS documentation and bundle math. |
| **Audyssey MultEQ / MultEQ-X** | First position is the main listening position; recommends at least 8 positions, minimum 3; more than about 8–10 in a small room rarely helps much; positions should remain around the listening area; height variation is recommended. citeturn24search1turn25view3 | Public user docs do not expose the internal weighting or confidence logic for feature acceptance. | Borrow the idea of a bounded listening cluster and diminishing returns beyond modest point counts. |
| **Dirac Live** | Public material states mixed-phase correction, nine measurement positions in common workflows, and that standard RC aims to meet the target response **on average** in the measured area; Bass Control adds inter-speaker low-frequency support, and ART focuses on roughly 20–150 Hz with stronger seat-to-seat control. citeturn31view1turn29search1turn31view2turn28search0 | Claims about “larger sweet spot” and “better timing” are partly product positioning unless tied to specific papers; public docs reviewed do not reveal the full candidate-selection logic. | Borrow the “optimize the measured area on average” framing and the idea that stronger spatial control needs multi-speaker cooperation, not just PEQ. |
| **Trinnov Optimizer** | Supports single-point and multi-point calibration; uses a unique reference point for delay/level/localization; exposes per-point and per-measurement weights from 0–100; requires all weighted points to be valid before computing filters. citeturn26view0turn26view2turn26view3 | Public docs say less about exactly how weights influence the optimizer objective. | Borrow explicit point weights and the concept of a protected reference point. |
| **Sonarworks SoundID Reference** | Uses 37 measurements around the sweet spot, with a weighted average, in a zone up to about 45 cm around the listening position. citeturn25view2turn23search7 | Public docs do not explain the weighting curve or confidence logic. | Borrow the idea that denser measurement mainly defines a listening **zone**, not a single point. |
| **RoomPerfect** | Measures both at the listening position and across the room; adds more room measurements to increase “RoomKnowledge”; recalculates Focus and Global filters as more room data are learned. citeturn24search6turn25view5turn25view6 | “RoomKnowledge” is a useful UX concept, but public docs do not reveal its exact statistical mapping. | Borrow the idea of a global confidence or “measurement knowledge” score. |
| **HouseCurve** | Explicitly says a single location should not be used for EQ; recommends average measurements of the listening area; for living rooms suggests 3–7 locations. citeturn25view0 | Averaging details are intentionally simple; not a full scientific confidence model. | Borrow its blunt honesty in the UI: “single point = not enough for global EQ.” |
| **Genelec GLM** | Official support docs emphasize calibrating distinct listening positions and storing them in separate system setups rather than advertising a broad multi-seat average. citeturn23search6turn23search10 | Public detail on GLM’s internal averaging or confidence logic is limited in the sources reviewed. | The lesson is mostly negative: position-specific monitor workflows are not the same use case as living-room zone correction. |
| **MSO** | Explicitly optimizes the modal region, aims to reduce seat-to-seat variation and flatten response, and stresses that unreliable measurements ruin the optimization. miniDSP’s MSO notes describe minimizing seat-to-seat variation and leaving later tonal shaping to a target-curve stage. citeturn36view0turn36view1turn35search14turn35search15 | MSO is for multi-sub optimization, not a general full-band smart-speaker EQ engine. | Borrow the measurement-discipline mindset and the separation between variance control and final voicing. |
| **DRC-FIR / rePhase open workflows** | DRC openly generates FIR correction filters from measurements; rePhase tutorials commonly rely on REW averaging and acoustic timing references before producing FIR corrections. citeturn37search3turn37search0turn37search6turn37search8 | Public, standardized “confidence” reporting is minimal; these are expert workflows rather than guided validity systems. | Borrow reproducibility and bundle completeness, not their UI assumptions. |

The practical takeaway is that the commercial tools reveal a lot about **measurement-area thinking** and **workflow safeguards**, but very little public detail about **feature-by-feature confidence math**. JTS should therefore avoid pretending there is a vendor-consensus formula for confidence. The real consensus is narrower and more valuable: measure a listening area, protect a main reference point, treat bass differently from mid/high, keep data quality strict, and never confuse a measurement artifact or a cancellation dip with a correctable room mode. citeturn25view3turn29search1turn26view2turn25view5turn7view0turn33view0

## Risks and edge cases

The biggest risk is **hiding geometry with EQ**. Toole explicitly says conventional EQ cannot reduce seat-to-seat bass variation and cannot fix loudspeaker directivity, while multiple subwoofers or coordinated multi-speaker methods can materially improve several seats. If JTS sees a broad, severe feature that is shared across seats but still produces large seat-to-seat spread, or if it sees mirrored left/right problems consistent with boundary placement, the correct output is a placement warning, not a bigger filter. Dirac Bass Control, ART, RoomPerfect, and MSO all indirectly reinforce this point by addressing low-frequency spatial consistency with **source cooperation**, not just per-channel PEQ. citeturn5view0turn4view0turn31view2turn28search0turn25view5turn36view0

The second risk is **misclassifying nulls or SBIR dips as correctable deficits**. Above the transition region, a deep notch may be a classic direct/reflected cancellation at the microphone. Even in the upper bass and lower mids, a boundary-interference dip may be broad enough to look tempting while still being mostly a geometry problem. JTS should therefore emit a placement/SBIR warning when a deep depression is broad but resists correction logic because of high seat variance, sign changes between seats, or strong channel asymmetry. That warning is especially important for stereo smart speakers near walls, shelves, or TVs. citeturn5view0turn5view3turn43view0

The third risk is **measurement corruption masquerading as acoustics**. REW warns about clipping, poor headroom, separate-device clock drift, and corruption from repeated sweeps without proper synchronization, while Audyssey and Trinnov insist on quiet rooms and no movement during measurements. MSO’s FAQ is blunt that if the measurements are unreliable, the optimization will not match reality. For JTS, that means the confidence engine must grade **measurement validity first** and only then grade acoustical confidence. A beautifully reasoned modal score built on a clipped sweep is still junk. citeturn33view0turn33view1turn25view3turn26view0turn36view1

A subtler edge case is **stereo imaging versus tonal averaging**. Ohl’s MMM paper explicitly says the method is about timbral averaging, not localization accuracy. That matters for JTS. A wide-area average may be excellent for tonal balance but poor for preserving the exact center-seat stereo image if the correction starts influencing higher-frequency channel differences. This is another reason to keep automated correction mostly low and low-mid, and to evaluate left/right similarity separately from average-seat tonal balance. citeturn20view0

## What to persist in the measurement bundle

To keep JTS replayable, auditable, and future-proof for FIR or AI explanation, the measurement bundle should preserve **more than the responses used to make today’s PEQ**. It should store the raw or deconvolved impulse responses for each channel and position, acquisition settings, quality metrics, all calibration metadata, and the exact deterministic thresholds used when decisions were made. That is the only way to support later “why did you reject this dip?” or “could a future FIR solver do better?” questions without re-measuring. The need for that depth is supported indirectly by Trinnov’s weighting/recompute model, RoomPerfect’s recalculation with added measurements, and MSO’s sensitivity to measurement discipline. citeturn26view2turn25view5turn36view1

A practical schema for the bundle should include these groups:

### Acquisition and hardware metadata

Store timestamps, sample rate, sweep settings, generated stimulus type and level, channel routing, timing-reference method, mic make/model, serial number where available, calibration-file checksum, speaker configuration, whether the run was mono or stereo, and the exact JTS software version and threshold profile. REW’s timing and sweep caveats make these parameters auditable necessities, not nice-to-haves. citeturn33view1turn33view4

### Position metadata

Store a reference listening point and each measurement position as explicit coordinates and height offsets relative to that point, plus any seat labels or user weights. If later versions add weighted areas or “focus seat” behavior, this becomes essential. Trinnov’s explicit point weighting and RoomPerfect’s distinction between focus and room measurements are clear prior art here. citeturn26view2turn25view5

### Raw and derived acoustical data

Store the per-position impulse response, frequency response, smoothing variants, and any derived minimum-phase or excess-phase representations. Also store ambient/noise captures if JTS performs them. If future FIR or multi-speaker solvers are added, these raw responses are far more valuable than only storing the current averaged curve. That is exactly the lesson implicit in DRC-FIR and rePhase workflows, which lean on complete measurement datasets rather than just a few exported PEQ points. citeturn37search3turn37search8

### Quality-control metrics

Persist clipping flags, pre/post-sweep headroom, timing-reference confidence, estimated delay, immediate repeatability metrics, noise/SNR estimates, and any corrupted-IR signatures. This should sit at the same level of importance as the acoustic data itself, because JTS’s confidence engine is fundamentally dual-layered: “is this measurement valid?” and then “is this feature worth correcting?” REW and MSO both strongly justify this split. citeturn33view0turn33view1turn36view1

### Decision log

Persist every candidate feature, whether it was accepted or rejected, along with the evidence and threshold crossings. For each accepted filter, store center frequency, gain, Q, confidence grade, affected seats, rationale text, and any warnings. For each rejected feature, store the reason: likely null, seat-local, too narrow, too high in frequency, poor repeatability, low SNR, and so on. This is the minimal structure that lets a future AI assistant explain the decision without inventing reasons after the fact. The public systems reviewed do not expose this level of auditability, which is precisely why it would be a differentiator for JTS. citeturn25view5turn26view2turn36view0

## Sources

Selected primary, official, and high-value technical sources used in this report are listed below. The URLs are provided in plain form, and the inline citations throughout the report point back to the supporting passages.

- Floyd E. Toole, “Measurement and Calibration of Sound Reproducing Systems.” `https://www.linkwitzlab.com/Toole-Room%20calibration.pdf` citeturn5view0turn5view2turn5view3  
- Floyd E. Toole, “Loudspeakers and Rooms for Sound Reproduction: A Scientific Review.” `https://audioroundtable.com/misc/Loudspeakers_and_Rooms.pdf` citeturn4view0  
- Fredrik Lingvall and Lars-Johan Brännmark, “Multiple-point statistical room correction for audio reproduction.” `https://doi.org/10.1121/1.3075615` citeturn12view0  
- Carl Hopkins, “On the efficacy of spatial sampling using manual scanning paths to determine the spatial average sound pressure level in rooms.” `https://doi.org/10.1121/1.3573986` citeturn22view0  
- Aki Mäkivirta et al., “Low-Frequency Modal Equalization of Loudspeaker-Room Responses.” `https://assets.ctfassets.net/4zjnzn055a4v/1PmYuXIBmuySLxSGG1iViz/bd768fcbeb5a5b2a4655793186a1973d/Low-Frequency_Modal_Equalization_Of_Loudspeaker-Room_Responses.pdf` citeturn16view0  
- Floyd E. Toole and Sean E. Olive, “The Modification of Timbre by Resonances: Perception and Measurement.” `https://pearl-hifi.com/06_Lit_Archive/15_Mfrs_Publications/Harman_Int%27l/AES-Other_Publications/Modification%20of_Timbre_by_Resonances.pdf` citeturn41view1turn41view3  
- Bruno Fazenda, Matthew Stephenson, and Andrew Goldberg, “Perceptual thresholds for the effects of room modes as a function of modal decay.” `https://doi.org/10.1121/1.4908217` citeturn46search2turn46search5  
- Mark R. Avis, Bruno M. Fazenda, and William J. Davies, “Thresholds of detection for changes to the Q factor of low-frequency modes in listening environments.” `https://eprints.hud.ac.uk/id/eprint/3551/` citeturn44search2turn45view0  
- REW Help: All SPL averaging methods. `https://www.roomeqwizard.com/help/help_en-GB/html/graph_allspl.html` citeturn7view0turn7view1turn7view2turn7view3  
- REW Help: measurement headroom, clipping, S/N, and timing reference. `https://www.roomeqwizard.com/help/help_en-GB/html/makingmeasurements.html` citeturn33view0turn33view1turn33view2  
- Audyssey MultEQ-X User Guide. `https://audyssey.com/MultEQ-X%20User%20Guide.pdf` citeturn25view3  
- Dirac Live public docs and Bass Control guide. `https://www.dirac.com/` and `https://www.stormaudio.com/wp-content/uploads/2024/10/Bass-Control-in-Live.pdf` citeturn31view1turn31view2turn29search1  
- Trinnov Optimizer reference manual. `https://www.trinnov.com/site/assets/files/1696/opt_refman_05_12_18_pro_sd.pdf` citeturn26view0turn26view2  
- Sonarworks Reference manual and support docs. `https://www.sonarworks.com/uploads/resourcekit/Studio%20Edition/Documents/Reference%204.1%20Manual.pdf` citeturn25view2turn23search7  
- RoomPerfect official pages and manuals. `https://lyngdorf.steinwaylyngdorf.com/roomperfect/` and `https://lyngdorf.steinwaylyngdorf.com/support-roomperfect/` citeturn24search2turn24search6turn25view5  
- HouseCurve measurement guidance. `https://housecurve.com/docs/usage/measuring` citeturn25view0  
- Multi-Sub Optimizer documentation. `https://www.andyc.diy-audio-engineering.org/mso/html/index.html` and `https://www.andyc.diy-audio-engineering.org/mso/html/reference-manual/faq.html` citeturn36view0turn36view1  
- DRC-FIR documentation. `https://drc-fir.sourceforge.net/` and `https://drc-fir.sourceforge.net/doc/drc.html` citeturn37search3turn37search0

### Open questions and limitations

A few things remain genuinely unresolved in the public record reviewed here. I did not retrieve public, source-level technical descriptions of the internal feature-selection or confidence logic used by Audyssey, Dirac, Sonarworks, GLM, or RoomPerfect; public documentation mostly describes measurement workflows, weighting concepts, or high-level goals rather than exact acceptance thresholds. Likewise, the literature does not yield one universally accepted mapping from seat-to-seat variance in dB to “safe to EQ” across all small-room geometries. JTS should therefore implement the thresholds above as **conservative, revisable policy defaults**, preserve the bundle so the policy can be replayed later, and present its confidence grades as engineering judgments grounded in the literature rather than as immutable psychoacoustic constants. citeturn25view3turn29search1turn26view2turn25view5