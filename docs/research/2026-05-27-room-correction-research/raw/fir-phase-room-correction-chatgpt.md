# FIR Readiness for JTS Room Correction

## Executive summary

JTS should treat FIR not as a blanket “better EQ,” but as a progressively enabled capability whose scope depends on what the measurement bundle can actually prove. Public documentation across REW, DRC-FIR, Acourate-style workflows, Trinnov, Dirac, and CamillaDSP converges on a few practical truths: room measurements are only partly minimum-phase; EQ is most trustworthy on broad, minimum-phase peaks and resonances; deep dips and interference nulls are usually poor correction targets; and higher-frequency room effects rapidly become position-sensitive, so “room correction” above roughly the room transition region becomes mostly direct-sound or loudspeaker correction rather than true room inversion. REW explicitly shows that flat excess group delay indicates minimum-phase regions, that low-frequency peaks are often minimum-phase while sharp dips often are not, and that room measurements are typically only minimum-phase in some regions. Toole likewise argues that non-minimum-phase interference dips should not be equalized, and that non-minimum-phase reflections in in-room measurements can make equalization actively problematic. DRC-FIR makes the same point algorithmically by limiting dips, using frequency-dependent windowing, and separating minimum-phase from excess-phase processing. citeturn22view0turn22view4turn31view1turn38view0turn38view3

For JTS, that means PEQ should remain the default outcome unless the measurement bundle includes enough evidence to justify something more. The first safe FIR milestone is not “automatic mixed-phase correction,” but **runtime FIR import/export plus validation**, followed by **minimum-phase FIR magnitude correction**, then **short FIR with explicit latency/headroom reporting**, then **FDW-conditioned FIR**, and only then **mixed-phase or excess-phase correction as an opt-in outcome gated by timing provenance, cross-seat stability, and pre-ringing risk checks**. That staged approach is consistent with how REW exposes minimum/excess phase and window settings, how DRC-FIR exposes minimum-phase extraction and excess-phase controls, how Acourate’s room macros start from FDW and only then expose excess-phase windows and pre-ringing compensation, and how Trinnov documents an explicit FIR/IIR split with configurable FIR length and optional phase optimization. citeturn24view0turn31view2turn28view0turn29search2turn25view2

On the runtime side, CamillaDSP is capable of serious FIR work on small hardware. Its documentation states that FIR uses convolution via FFT/IFFT, that segmented convolution is used when taps exceed the chunk size, and that larger chunk sizes reduce CPU cost for long FIR filters at the expense of latency. CamillaDSP also publishes suggested starting chunk sizes of 1024 for 44.1/48 kHz and 2048 for 88.2/96 kHz, both about 22 ms per chunk, and its public benchmark includes a Raspberry Pi 4 doing 8 channels of 262k taps at 192 kHz at about 55% CPU. Raspberry Pi 5’s official specifications say the BCM2712 Cortex-A76 platform is up to three times faster than Pi 4, so a 2-channel JTS room-correction pipeline at 48 or 96 kHz with sane tap counts should be feasible on a Pi 5 1 GB. That performance conclusion is an inference, not a published JTS-specific benchmark, and it should be verified on-device with observability and rollback. citeturn17view0turn21view0turn20search0turn19search0turn19search8

The most important product decision is therefore not “should JTS support FIR,” but **“what should JTS refuse to do automatically?”** The validator should explicitly surface states such as **PEQ-ready**, **minimum-phase FIR-ready**, **short linear-phase FIR-ready with latency warning**, and **mixed-phase unsafe**. Those states should be driven by documented artifacts: raw or reproducible sweep capture, deconvolved impulse response, complex transfer function, phase and unwrapped phase, group delay and excess group delay, precise window settings including FDW, timing-reference provenance, and multi-position variance. REW’s API directly exposes most of those artifacts, including frequency response with phase, group delay, impulse response with timing reference description, and IR window settings. citeturn24view0turn22view2turn22view3turn22view5

## When FIR is justified and what the correction modes mean

**Public facts.** FIR is justified when the goal extends beyond a bounded set of parametric filters and into controlled convolution-based shaping. CamillaDSP’s own room-correction example describes FIR correction as a convolution filter loaded from an impulse-response file, while its engine documentation states that FIR filtering is implemented with convolution via FFT/IFFT and segmented convolution for longer filters. REW and rePhase both support FIR-centric workflows, with REW able to generate minimum-phase versions and expose excess phase, and rePhase positioned explicitly as a FIR generation tool for linear-phase crossovers and phase compensation. DRC-FIR goes further by explicitly separating minimum-phase and excess-phase processing, dip limiting, and filter extraction. citeturn9view4turn17view0turn24view0turn44search0turn44search2turn31view1turn31view2

**Inference for JTS.** FIR is actually justified over IIR/PEQ in JTS when at least one of these is true: the correction needs more spectral detail than bounded PEQ can express cleanly; the target needs sample-rate-specific export or shared interoperability with external convolver ecosystems; the system wants a minimum-phase FIR variant that follows a complex, regularized target more smoothly than a small PEQ set; the workflow needs FDW-conditioned correction so that direct sound dominates high-frequency design; or the project is correcting **documented** loudspeaker/driver timing or low-frequency excess phase rather than attempting to “fix” seat-specific combing and nulls. It is **not** justified when the visible problems are mainly narrow dips, high-frequency seat-to-seat interference, or late reflections. REW, Toole, and CamillaDSP’s own room-correction example all point in that direction. citeturn22view0turn38view0turn38view3turn9view4

The practical mode differences are these:

**Magnitude-only correction** means designing the target around the amplitude response and letting the chosen filter topology carry whatever phase behavior it inherently introduces. In practice this is what most conservative PEQ workflows do, and it is also what a minimum-phase FIR often approximates. REW’s help notes that if a system is minimum-phase, correcting magnitude also corrects the corresponding phase relationship; DRC-FIR’s minimum-phase extraction stage similarly describes a minimum-phase filter as correcting magnitude and the minimum-phase part of the phase response. citeturn22view4turn31view2

**Minimum-phase correction** means using the portion of the response whose phase is determined by magnitude, usually after removing pure delay. REW computes minimum phase from magnitude using the real cepstrum and exposes excess phase as the difference between measured and minimum phase. DRC-FIR and rePhase both support minimum-phase workflows; DRC-FIR even exposes a zero-delay minimum-phase extraction stage, while rePhase and its ecosystem describe minimum-phase gain EQ as the usual choice for loudspeaker-range amplitude flattening. citeturn22view4turn22view0turn31view2turn44search5

**Linear-phase FIR** means a symmetric FIR with constant group delay across frequency. MathWorks states that a symmetric linear-phase FIR has constant group delay equal to half the filter order, and the DAFx paper on a quasi-linear-phase equalizer notes that minimum-phase equalizers have the smallest possible latency and no pre-ringing, whereas linear-phase equalizers preserve phase relationships at the cost of more delay. In room correction, linear-phase FIR is most defensible when preserving phase relationships or building crossovers/driver alignment matters more than latency, and when pre-ringing has been explicitly risk-checked. citeturn45search1turn45search3turn35view0

**Mixed-phase or excess-phase correction** means correcting some part of the phase that is not implied by the magnitude response alone. REW exposes excess phase and excess group delay for exactly this reason. DRC-FIR has separate excess-phase prefiltering and flattening controls. Dirac publicly describes itself as mixed-phase. Trinnov documents a separate phase-optimization switch plus a hybrid FIR/IIR design. Acourate’s room macros document explicit excess-phase windows and pre-ringing compensation. Publicly, then, mixed-phase correction is real and common; what is less public in proprietary systems is the exact decision logic. citeturn22view0turn22view4turn30view0turn25view1turn25view2turn29search2

**Frequency-dependent windowed correction** is not a separate phase class so much as a measurement-conditioning step that heavily influences what FIR should be allowed to do. REW defines FDW as a Gaussian window whose width narrows with increasing frequency and states that it is applied after the left/right impulse windows, centered on the window reference time, ideally the impulse peak. Acourate’s Room Macro 1 says correction-filter generation should exclude reflections using FDW and include only the direct sound from the loudspeaker in the calculation. DRC-FIR’s documentation similarly frames frequency-dependent windowing as defining the part of the time-frequency plane that is safe to correct. citeturn23view0turn22view1turn28view0turn31view0

The key JTS conclusion is that **FDW should be viewed as a prerequisite for trustworthy upper-band FIR design, not as an optional embellishment**. Without it, the system is far more likely to chase reflections or seat-specific comb filtering above the low-frequency region. citeturn23view0turn28view0turn31view0

## Recommended staged FIR ladder for JTS

The safest ladder for JTS is a capability ladder, not a feature flag list. Each stage should only unlock if the validator can explain why it is safe.

**Stage one: FIR runtime import and export only.** Publicly, CamillaDSP already accepts FIR correction filters as convolution files, and REW exposes impulse-response artifacts and filter IR export mechanisms through its UI/API. This stage gives JTS interoperability and bundle replay without committing to auto-generation. It is low risk and immediately useful for developer testing, external tool users, and reproducibility. citeturn9view4turn24view0turn17view0

**Stage two: minimum-phase FIR for magnitude correction.** This is the first stage that should be generated automatically. REW, DRC-FIR, and rePhase all point to minimum-phase correction as the safest causally behaved FIR mode for routine equalization. DRC-FIR even describes the minimum-phase extraction as “almost artifacts free” and basically zero-latency. For JTS, this stage should still keep the existing PEQ guardrails: bounded boost, no null chasing, broad-feature preference, and target-curve discipline. citeturn22view4turn31view2turn44search5

**Stage three: short FIR with latency and headroom reporting.** CamillaDSP’s block processing means chunk size already creates a baseline live-path delay, and linear-phase FIR adds its own group delay. CamillaDSP’s FAQ also warns that attenuation-only filtering can still increase peak sample amplitude and recommends additional gain reduction, such as -3 dB, when clipping appears. JTS should therefore not apply short FIR silently: the report should name chunk size, estimated FIR delay, expected added headroom requirement, and whether hot reload changes coefficient file names cleanly for live updates. citeturn21view0turn15view4

**Stage four: FDW-conditioned FIR.** Once JTS stores explicit IR windows and timing provenance, it can generate FIR from an FDW-conditioned response. REW and Acourate both make clear that this is how one deliberately reduces the influence of late reflections at higher frequencies. For JTS, this stage is the point where the system starts distinguishing “speaker/direct-sound shaping” from “room correction,” and the UI should say so. citeturn23view0turn28view0

**Stage five: mixed-phase or excess-phase correction as opt-in.** This stage should remain opt-in even after validation passes. Public tools expose it as powerful but parameter-sensitive: REW surfaces excess phase and excess group delay; DRC-FIR provides dip limiting, excess-phase flattening, and explicit pre-echo handling; Acourate exposes excess-phase windows and pre-ringing compensation with iterative testing; Trinnov exposes a phase-optimization switch and FIR-length tuning. That is not the behavior of a feature that should auto-fire on mediocre phone captures. citeturn22view0turn30view0turn31view1turn29search2turn25view2

**Inference for JTS.** The default ladder outcome should probably be:

- PEQ-only when the bundle lacks high-confidence phase/timing provenance.
- Minimum-phase FIR when the response is stable enough to justify denser convolution shaping but not phase inversion.
- Short linear- or asymmetric-phase FIR only when the user has explicitly accepted latency and the pre-ringing score is low.
- Mixed-phase only when the validator sees documented timing reference, strong cross-position agreement in the target band, broad excess-phase structure rather than narrow seat-specific structure, and clean verification opportunity.

That keeps the system explainable and easy to roll back. The “best” algorithmically ambitious result is not always the best product decision. citeturn22view2turn22view4turn31view0turn31view1turn31view2

## Measurement artifact checklist

**Public facts.** REW’s API and help system are unusually useful here because they map closely to what JTS should persist. REW exposes: frequency response with magnitude and phase; group delay; impulse response with sample rate, start time, and **a description of the timing reference used**; and IR window settings including left/right windows, reference time, and FDW parameters. REW’s graphs also distinguish raw phase, unwrapped phase, minimum phase, excess phase, and group delay, and document that phase-bearing arithmetic and averaging require meaningful time alignment. citeturn24view0turn22view4turn22view3turn22view5turn42view0

From that public baseline, the minimum responsible JTS bundle before any FIR generation should contain the following artifacts:

| Artifact | Why it matters | Minimum for JTS |
|---|---|---|
| Raw capture or reproducible sweep record | Lets JTS re-run deconvolution and detect measurement bugs, clipping, and pipeline regressions. Impulse response fully characterizes the measured system, but preserving the pre-deconvolution path improves replay and observability. citeturn23view0turn24view0 | Strongly recommended for all FIR stages; mandatory for mixed-phase if JTS owns the measurement path. |
| Deconvolved impulse response | The core time-domain artifact from which frequency response, windows, ETC, and timing are derived. citeturn23view0turn22view5 | Mandatory. |
| Complex transfer function | Magnitude alone is insufficient for phase-aware decisions. REW’s API exposes magnitude plus phase directly. citeturn24view0turn22view4 | Mandatory. |
| Wrapped and unwrapped phase | Unwrapped phase is needed for interpretation, delay estimation, and excess-phase work. REW documents both views and IR delay estimation based on minimum-phase comparison. citeturn22view4 | Mandatory for any phase-aware stage. |
| Group delay and excess group delay | REW explicitly uses excess group delay to identify minimum-phase regions and to reveal sub/main timing offsets. citeturn22view0turn22view3 | Mandatory for validator decisions. |
| Window settings | Left/right windows and FDW dramatically change what is being corrected. REW and Acourate both treat them as first-class design parameters. citeturn23view0turn24view0turn28view0 | Mandatory. |
| Timing-reference provenance | REW documents loopback reference, acoustic reference, or no reference, and notes that timing reference removes variable computer/interface delay. citeturn22view2turn24view0 | Mandatory for mixed-phase; important for any averaging. |
| Multi-position responses and variance | REW distinguishes vector average from RMS/dB averages and warns that phase-aware averaging should be time aligned; spatial averages should often use SPL alignment first. citeturn42view0 | Mandatory for any auto-correction intended for more than one seat. |
| Noise, distortion, and clipping indicators | REW exposes distortion and notes measurement noise floor; JTS should also record ADC clipping/overload events from capture. citeturn41search6turn23view0 | Mandatory for acceptance. |

**Inference for JTS.** I would add four bundle fields that are not acoustics-theory artifacts but are essential product artifacts:

The first is **measurement pipeline provenance**: app version, DSP version, sweep settings, smoothing choice, deconvolver version, phone/browser model if available, and calibration identifiers. The second is **seat geometry metadata**: named point labels, rough position coordinates or relative offsets, and whether the user wanted single-seat precision or area optimization. The third is **design lineage**: source traces, preprocessing chain, target curve, regularization, rejected candidates, and chosen filter. The fourth is **verification linkage**: before/after bundle IDs and whether the verification was captured with identical timing and window settings. Those are product inferences, but they are directly supported by the fact that the public tools which do this well make windows, timing, alignment, and averaging explicit rather than implicit. citeturn24view0turn42view0turn28view0

A very practical rule emerges from the literature and tool behavior: **do not collapse artifacts too early**. Keep per-position, per-channel complex responses before averaging. Mixed-phase decisions made from a prematurely averaged response are much harder to trust. REW’s own distinction between vector averaging and magnitude averaging is a reminder that the averaging choice changes the physics of the resulting object. citeturn42view0

## FIR readiness validator design

The validator should be opinionated, but it should never be mysterious. The output state should be both machine-usable and user-readable.

A useful state model for JTS would be:

- **PEQ-ready**
- **Minimum-phase FIR-ready**
- **Short FIR-ready with latency warning**
- **FDW FIR-ready**
- **Mixed-phase provisional**
- **Mixed-phase unsafe**

**Public facts.** REW gives a practical test for minimum-phase regions: where excess group delay is flat, the response is minimum-phase in that region. REW also notes that low-frequency peaks are often minimum-phase and that sharp dips often correspond to non-minimum-phase behavior and poor EQ results. Toole separately states that non-minimum-phase interference dips cannot be corrected by equalization. DRC-FIR’s design agrees, since it explicitly performs dip limiting to avoid unstable or excessive inversion. citeturn22view0turn38view0turn31view1

**Inference for JTS.** The validator should therefore score the response in **bands**, not once for the whole spectrum. A single global “FIR yes/no” flag is too blunt. A good banded score would combine:

- **Timing confidence**: loopback reference > acoustic reference > no reference.
- **Phase trustworthiness**: low unwrap discontinuity rate, plausible excess group delay, and no obvious delay ambiguity.
- **Minimum-phase consistency**: measured phase stays close to the phase predicted from smoothed magnitude after pure delay removal, or equivalently excess group delay remains locally flat enough.
- **Spatial stability**: cross-seat variance below threshold in the candidate correction band.
- **Boost safety**: no large gain demand caused by deep notches or regularization-sensitive inversions.
- **Pre-ringing risk**: estimated from intended FIR symmetry, target sharpness, and predicted energy before the main impulse.
- **Verification potential**: whether the bundle is complete enough to re-measure and attribute any failure.

A conservative validator message should look like this:

**PEQ-ready.** Broad low-frequency peaks are stable across seats; timing provenance is incomplete; several response dips appear non-minimum-phase; apply bounded cuts only. Supported by REW’s minimum-phase guidance and Toole’s warning on non-minimum-phase dips. citeturn22view0turn38view0

**Minimum-phase FIR-ready.** Timing is consistent enough for causal convolution; the target band is dominated by minimum-phase structure; required boost stays within guard limits; multi-position variance is acceptable. Supported by REW minimum/excess phase analysis and DRC-FIR minimum-phase extraction behavior. citeturn22view0turn31view2

**Mixed-phase unsafe.** No valid timing reference, large seat-to-seat phase disagreement, and candidate correction would require inversion near deep dips or high pre-ringing risk. Supported by REW timing-reference documentation, REW averaging caveats, and DRC-FIR’s explicit pre-echo and dip-limiting safeguards. citeturn22view2turn42view0turn31view0turn31view1

In other words, the validator should report **reasons**, not just a label. That is also the only way to make bundle replay and support diagnostics worthwhile.

## Algorithms and pseudocode

The following logic is not publicly documented as a single algorithm by any one tool. It is a JTS design inference built from the documented pieces above: REW’s minimum/excess phase and timing model, DRC-FIR’s dip limiting and pre-echo caution, Acourate’s FDW-first preparation, and CamillaDSP’s runtime realities. citeturn22view0turn22view2turn24view0turn31view0turn31view1turn28view0

```text
function validate_bundle(bundle):
    require impulse_response
    require complex_response
    require per_position_measurements
    require window_settings

    timing_score = score_timing(bundle.timing_reference)
    noise_score  = score_noise_and_clipping(bundle)
    spatial_score = score_spatial_variance(bundle.per_position_measurements)

    for each analysis_band:
        mp_score = score_minimum_phase_likelihood(
            excess_group_delay_flatness(bundle, analysis_band),
            phase_vs_minphase_error(bundle, analysis_band),
            dip_depth_and_width(bundle, analysis_band)
        )

        boost_score = score_boost_safety(
            predicted_inverse_gain(bundle, analysis_band),
            notch_regularization_sensitivity(bundle, analysis_band)
        )

        phase_score = score_phase_trust(
            unwrap_stability(bundle, analysis_band),
            timing_score
        )

        fdw_score = score_window_appropriateness(
            bundle.window_settings, analysis_band
        )

        band_score[analysis_band] = combine(
            timing_score, noise_score, spatial_score,
            mp_score, boost_score, phase_score, fdw_score
        )

    return classify(band_score, timing_score, spatial_score)
```

A practical “minimum-phase-safe-to-correct” heuristic would look like this:

```text
function safe_for_minphase_correction(band):
    if band.has_deep_narrow_dip: return false
    if band.excess_group_delay_is_not_flat: return false
    if band.seat_to_seat_variance_is_high: return false
    if predicted_boost_exceeds_guardrail: return false
    return true
```

And the stage selector should be deliberately simple:

```text
function choose_stage(validation):
    if validation.bundle_incomplete:
        return "PEQ-ready"

    if validation.timing_reference_missing or validation.phase_untrusted:
        if validation.magnitude_features_are_broad_and_stable:
            return "minimum-phase FIR-ready"
        return "PEQ-ready"

    if validation.phase_trusted and validation.spatial_stability_good:
        if validation.pre_ringing_risk_low and validation.latency_budget_allows:
            if validation.fdw_present:
                return "FDW FIR-ready"
            return "short FIR-ready with latency warning"

    if validation.excess_phase_is_broad_stable_and_verifiable:
        return "mixed-phase provisional"

    return "mixed-phase unsafe"
```

The missing piece is the pre-ringing score. A useful engineering approximation is to compute it from the **designed filter itself** before deployment:

```text
function pre_ringing_risk(fir):
    peak = index_of_main_peak(fir.impulse)
    pre_energy  = energy(fir.impulse[0:peak])
    post_energy = energy(fir.impulse[peak:end])
    pre_ratio_db = 10*log10(pre_energy / post_energy)

    step_undershoot = max_negative_before_step_settle(fir.step_response)
    early_osc_cycles = dominant_cycles_before_peak(fir.impulse)

    if pre_ratio_db > threshold_1 or step_undershoot > threshold_2:
        return "high"
    if fir.is_linear_phase and fir.has_narrow_highQ_or_large_low_freq_boost:
        return "elevated"
    return "low"
```

That is a JTS inference, but it follows the documented physics: pre-ringing comes from energy before the main spike, and DRC-FIR explicitly treats pre-echo as something the ear is sensitive to and therefore constrains it with shorter excess-phase windowing. citeturn35view0turn31view0turn30view4

## CamillaDSP and Raspberry Pi runtime considerations

**Public facts.** CamillaDSP’s engine documentation says FIR uses convolution via FFT/IFFT, with segmented convolution when the number of taps exceeds the chunk size. It recommends chunk sizes that are “easy” powers of two because that speeds up the FFT in the convolution filter, and gives starting values of 1024 for 44.1/48 kHz, 2048 for 88.2/96 kHz, and 4096 for 176.4/192 kHz. Those suggested chunk sizes all correspond to about 22 ms per chunk. The same documentation states that increasing chunk size can reduce CPU usage for long FIR filters, but at the cost of more latency. CamillaDSP also documents queue-memory scaling as `2*chunksize*queuelimit` samples per channel, with maximum RAM scaling accordingly. In older-but-still-official documentation and the README lineage, CamillaDSP also reports a Raspberry Pi 4 example doing 8 channels with 262k taps per channel at 192 kHz at about 55% CPU. Raspberry Pi 5’s official product brief specifies a 2.4 GHz quad-core Cortex-A76 processor and says performance is up to three times that of the previous generation. citeturn17view0turn21view0turn20search0turn19search0turn19search8

That yields the following practical examples for JTS. The FIR delay examples use the standard linear-phase FIR result that a symmetric FIR of order *n* has constant group delay *n/2* samples, i.e. about `(N-1)/(2*fs)` seconds for an *N*-tap filter. citeturn45search1turn45search3

| Sample rate | Chunk size | Chunk duration | 2048-tap linear-phase FIR delay | 4096-tap delay | 8192-tap delay |
|---|---:|---:|---:|---:|---:|
| 48 kHz | 1024 | 21.3 ms | 21.3 ms | 42.7 ms | 85.3 ms |
| 96 kHz | 2048 | 21.3 ms | 10.7 ms | 21.3 ms | 42.7 ms |

Those numbers matter in two different ways. The **chunk duration** is the operational live-path granularity CamillaDSP recommends for stable convolution. The **FIR delay** is the filter’s acoustic/group-delay consequence if the filter is symmetric linear-phase. In a real JTS capture-playback path, users will experience some combination of DSP block latency, buffer target level, and any filter delay the design itself introduces. That is why “short FIR” deserves its own stage and its own report. citeturn21view0turn35view0turn45search1

**Inference for JTS.** On a Pi 5 1 GB, I would start with these product constraints:

At 48 kHz, treat **2048 to 8192 taps per channel** as the safe first deployment envelope for room correction. At 96 kHz, start more conservatively, because the same acoustic-time delay requires twice as many taps. Use larger tap counts only after on-device profiling confirms CPU margin and thermal stability. This is not because the Pi 5 is weak; it is because JTS also cares about resilience, hot reloads, and observability on a 1 GB system, not merely raw convolution throughput. The published Pi 4 benchmark suggests ample headroom for 2-channel JTS use, but that benchmark was at a different channel count and setup, so it should be taken as encouraging context, not proof. citeturn20search0turn19search0

JTS should also explicitly compute and log **headroom reserve**. CamillaDSP’s FAQ notes that even all-negative-gain filters can create higher sample peaks because phase relationships change, and recommends additional gain reduction when clipping occurs. So every designed FIR bundle should store predicted peak gain, suggested preamp trim, and whether JTS auto-applied it. citeturn15view4

Finally, JTS should remember that CamillaDSP hot reload behavior is not arbitrary: older engine docs note that to update FIR coefficients via config reload, the coefficient filename must change. For bundle replay, versioned filenames are therefore a feature, not a nuisance. citeturn21view0

## Pre-ringing, prior art, sources, and unresolved risks

Audible pre-ringing is not mysterious. It is what happens when the correction filter places significant energy **before** the main impulse peak, which is intrinsic to symmetric linear-phase filtering and can also appear when excess-phase inversion is unconstrained. The DAFx equalizer paper states that minimum-phase equalizers have the smallest possible latency and do not produce pre-ringing because the impulse response is zero before the main spike; by contrast, linear-phase processing preserves phase relationships at the cost of more delay. DRC-FIR explicitly discusses pre-echo truncation and says a much shorter window is used because the ear is sensitive to pre-echo. REW’s tooling and the rePhase ecosystem both point users toward first aligning time, then cautiously applying phase correction, and the Acourate macro description explicitly includes pre-ringing compensation. citeturn35view0turn31view0turn30view4turn43view1turn29search2

**Inference for JTS.** Software should flag pre-ringing risk when any of the following are true: the FIR is symmetric and long; the target contains sharp, narrow, or high-boost corrections; the requested correction attacks deep dips; the excess-phase correction band is broad without strong timing confidence; or the designed impulse has a high pre-peak energy ratio or visible step-response undershoot. The simplest product rule is: **if JTS cannot explain where the pre-peak energy came from, it should not ship the filter automatically**. That pairs naturally with three avoidance strategies: prefer minimum-phase FIR for ordinary magnitude correction, confine excess-phase work to low-frequency broad structures with trusted timing, and apply FDW or equivalent windowing so high-frequency correction follows direct sound instead of late reflections. citeturn23view0turn31view0turn28view0turn22view2

The public prior-art picture is summarized below.

| Tool or system | Publicly documented behavior most relevant to JTS | What JTS should learn |
|---|---|---|
| **REW** | Exposes frequency response with phase, group delay, impulse response with timing-reference description, IR windows and FDW settings, minimum-phase generation via real cepstrum, and excess phase / excess group delay. It also distinguishes vector averaging from magnitude averaging and documents timing alignment requirements. citeturn24view0turn22view4turn22view0turn42view0 | Use REW’s artifact model as the template for JTS bundles and validation. |
| **rePhase** | Officially a FIR generation tool for fully linear-phase active crossovers and loudspeaker phase compensation; public descriptions note support for linear-phase and minimum-phase gain EQ, phase EQ, and REW filter import. Tutorials on the official site emphasize time alignment before phase EQ and caution that phase linearization is mainly for intrinsic speaker behavior, not reflections. citeturn44search0turn44search2turn44search10turn43view3 | Let JTS import/export this style of workflow before attempting to automate it fully. |
| **DRC-FIR** | Separates minimum-phase and excess-phase processing, uses dip limiting and frequency-dependent windowing, recommends typical correction filter lengths from 8192 to 65536 taps, and offers a minimum-phase extraction stage that is “almost artifacts free” and basically zero latency. It also explicitly treats pre-echo as a constrained problem. citeturn31view0turn31view1turn31view2 | DRC-FIR is the clearest open-source template for a guarded FIR ladder. |
| **Acourate** | Public wiki says Room Macro 1 starts from measured pulse responses, smoothing, and FDW so only direct sound is included; Macro 4 publicly exposes excess-phase windows and pre-ringing compensation. Official product copy says Acourate corrects timing errors of room and speakers by phase correction. citeturn28view0turn29search2turn27search1turn29search10 | Phase correction should come only after disciplined measurement conditioning. |
| **Audiolense** | Official product pages describe frequency correction, optional time-domain correction, and “True Time Domain” behavior that synchronizes first arrival across frequencies and speakers in the XO tier. citeturn28view3turn28view4 | A staged product tiering model is plausible and user-comprehensible. |
| **Dirac Live** | Public white paper describes mixed-phase filtering, multiple-position measurement, impulse/frequency optimization, and proprietary low-computation filter structures, but gives limited public algorithmic detail. citeturn25view1 | Mixed-phase is market-legible, but proprietary systems reveal little about validator logic. |
| **Trinnov** | Public manual documents optional phase optimization, a hybrid FIR/IIR structure, configurable FIR length with a default 20 ms FIR, and explicit IIR-band deployment. citeturn25view2 | Hybrid correction with latency-conscious FIR limits is a serious-production pattern. |
| **CamillaDSP** | Official docs describe convolution FIR support, segmented convolution, recommended chunk sizes, queue behavior, hot reload considerations, and room-correction workflows. citeturn17view0turn21view0turn9view4 | JTS can implement a disciplined FIR runtime today; the gating problem is measurement quality, not engine capability. |
| **CamillaFIR / DecayCore** | Public site signals an emerging open-source design philosophy centered on phase-aware correction, temporal decay control, conservative boost limits, and guarded automatic decisions. citeturn40search0turn40search4turn40search6 | Worth monitoring as adjacent prior art, but not yet foundational enough to drive JTS policy on its own. |

The strongest documented sources underlying this report are REW’s help/API corpus, CamillaDSP’s official documentation, DRC-FIR’s manual, Trinnov’s reference manual, official or quasi-official rePhase descriptions, the Acourate wiki, the Dirac white paper, Floyd Toole’s room-calibration paper, and the classic Neely-Allen paper on room-response invertibility. Those citations are embedded above and function as the linked source list for this report. citeturn24view0turn21view0turn31view0turn25view2turn44search0turn28view0turn25view1turn38view0turn38view2

The biggest unresolved risks for JTS are not mathematical; they are product and measurement risks. Phone/browser captures may be adequate for conservative PEQ while still being insufficient for excess-phase claims. Public documentation for proprietary systems is shallow on exactly how they decide when phase correction is safe, so any JTS automation here will necessarily involve inference. And while the Pi 5 hardware looks ample for sensible 2-channel FIR workloads, JTS still needs its own benchmarks for thermal stability, long-session reliability, coefficient hot-swapping, and “worst bundle” memory behavior on the 1 GB target. Those are the right next unknowns to keep open. citeturn22view2turn25view1turn25view2turn19search8turn20search0