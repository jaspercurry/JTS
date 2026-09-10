# ADR-0289: Leveling performance requires observed evidence

- **Date:** 2026-09-10
- **Status:** Accepted for the evidence changes described below; controller optimization and shared acquisition remain unimplemented.
- **Scope:** Shared measurement-quality audit [#4868](https://github.com/jaspercurry/JTS/issues/4868), audited code `5fc22ba456bf480b6d875d1828f0f864d29f42c6`.

## Decision

Keep the existing continuous-playback, measured-gap leveler while exposing enough evidence to measure its performance. Do not replace it with stepped bursts or shorten its settling windows on the assumption that a commercial product is faster. Add controller timing and meter-scope receipts to the existing result, publish them on successful CLI calls, reject nonfinite recordings in the shared quality assessor, and include recorded loudness compensation in matched-comparison context.

These are preparatory implementation changes. They do **not** establish fresh acoustic level at the start of a measurement group, implement shared retries, verify capture gain, or make every completed capture trustworthy. Those requirements remain in #4863–#4865. No JTS3 speed or quality improvement is claimed. The live benchmark required to select the smallest control change was unavailable: `jts3.local` did not resolve from the development environment. There was no playback or deployment.

The research supports a candidate direction: reuse the current correction calculation inside one held measurement group, use a saved reference only to propose an admissible starting setting, then acquire current acoustic evidence and confirm. That direction is an engineering inference, not a published commercial algorithm or an implemented controller change. Warm starting, probe spectrum, and shorter settling remain hypotheses to test against a measured JTS3 baseline.

## Published methods and evidence strength

The sources below are primary product documentation or original research. A documented level-check duration is not a measured time-to-target, and instructions for manual adjustment do not establish an automatic feedback algorithm. Documentation was accessed on 2026-09-10. Undated pages and beta documentation describe the retrieved version only; proprietary update laws and undisclosed timings remain unknown.

| System/source | Published behavior | Timing evidence and relevance to JTS |
|---|---|---|
| REW, Check Levels | Pink-noise calibration around 75 dB SPL; subsequent measurements keep the selected stimulus and processor level. USB microphone digital levels can legitimately be low. Input-gain problems should not be solved by continually raising playback. [^1] | This is an operator-assisted procedure, not evidence of automatic convergence speed. It supports treating output level, input level, and measurement quality separately. |
| REW, measurement dialog | Its band-dependent pink-noise check stops automatically after three seconds and reports RMS. Same Sweep Level is required for comparisons. [^2] | Three seconds is a published probe duration, not an end-to-end autolevel benchmark. |
| Genelec GLM standard loudness calibration | An internal high-pass pink-noise generator is adjusted to the selected SPL for a specified source loudness. A fader SPL indication remains meaningful only for the assumed source level and playback chain. [^3] | No convergence time, step count, settling estimator, or control law is published in the cited article. The source-dependence directly supports recording stimulus amplitude as well as Main gain. |
| Audyssey MultEQ-X v1.0 | Subwoofer level matching uses band-limited pink noise, an approximate 75 dB target with a ±3 dB band, and a deliberately slow display. The manual says acquisition takes several seconds and to wait at least five seconds after changing gain. [^4] (p.17) | This is evidence against assuming every commercial adjustment is nearly instantaneous. The retrieved v1.0 manual is a historical primary source, not a claim about the latest release. |
| Dirac Live on miniDSP Tide16 | The documented preparation is to begin with low master output, use pink noise, and manually obtain appropriate microphone/output levels. Low-frequency background noise can impair calibration. [^5] | No automatic time-to-target is established. The microphone and playback controls have different jobs. |
| miniDSP SHD AutoEQ | Its level-check instructions start Master at −60 dB and the test signal at −20 dB, then ask the operator to adjust. The selected measurement frequency range applies to both pink noise and sweeps. [^6] | Useful evidence for purpose-specific probes; no evidence of a faster closed-loop leveler. |
| Lyngdorf RoomPerfect | Its documented measurement uses dedicated low- and high-frequency signals and measurements around the room. [^7] | The cited page does not publish an autolevel update law or convergence time. Its different measurement strategy is not a basis for a speed ranking. |

REW also documents periodic pink-noise signals whose energy is distributed over FFT bins, and calibration presets of 500–2000 Hz for speakers and 30–80 Hz for subwoofers. Its periodic-noise benefits rely on an appropriate measurement method; coherent measurements require compatible clocks. Published crest factors for its synthesized signals do not describe JTS's existing noise generator. [^8] JTS currently generates deterministic **band-limited white noise**, filtered with a fourth-order Butterworth bandpass and peak-normalized. Calling that signal pink noise would be incorrect.

Farina's original swept-sine work supports separating a linear impulse response from harmonic responses for approximately time-invariant nonlinear systems. The accessible AES abstract does not establish a fast acoustic level-control method. [^9] Stan, Embrechts, and Archambeau compare MLS, inverse repeated sequences, time-stretched pulses and logarithmic sweeps; their experiments show that the preferred excitation depends on noise and nonlinearity. Their findings do not establish one universally best short probe. [^10] A leveling signal and a distortion/response measurement signal therefore need not be the same waveform.

## Estimation and fast feedback

For a fixed, sufficiently linear playback/capture path and the same signal spectrum, the first estimate is `new Main = current Main + target SPL − observed SPL`. This follows from logarithmic level units, not from reverse engineering a commercial product. Current JTS code already uses this error estimate and caps positive corrections; it does not merely add a fixed small increment without observing the mic. When a trusted observation permits a nearly complete correction, one correction plus confirmation can be faster than many bounded steps. That potential improvement depends on the quality of the initial observation.

Background noise makes an initial low-level observation unreliable. In linear power units, an uncorrelated stationary background can be subtracted: `signal power = active power − ambient power`; corresponding SNR is `10 log10(signal power / ambient power)`. Subtracting two dB readings is a signal-plus-noise ratio, not that noise-subtracted SNR. A 6 dB active-to-ambient difference implies approximately 4.7 dB signal-to-noise ratio under those assumptions. JTS's current 6 dB rise guard demonstrates observability, not enough evidence for a frequency-response tuning decision.

When active and ambient powers are close, small background changes dominate the subtraction. A negative or unstable difference means unavailable signal evidence. Do not manufacture a giant correction from it. At low excitation, use bounded admissible observations; when a transient is detected, wait for a new quiet interval and retry at the same input. Persistent lack of SNR may justify a different admitted excitation or longer measurement only when the experiment permits it. Neither a transient nor a quiet USB-mic dBFS reading alone justifies increasing output.

The entire local path matters: command/readback, stimulus enqueue, fan-in and DSP buffering, output transport, DAC, acoustic propagation and decay, microphone buffering, and estimator response. The wired level meter's 2048-frame period at 48 kHz corresponds to about 42.7 ms per block; that is not an end-to-end latency measurement. ALSA explicitly distinguishes playback delay to DAC output from capture delay between ADC sampling and reading a frame. [^11] Measure acoustic onset and delivered samples before reducing a post-change exclusion interval.

Continuous playback avoids repeatedly starting processes and replay chains at every observation. Short bursts can provide intervening ambient windows, but they also add startup, fade and decay intervals. Select them only if measured transient rejection or hardware behavior warrants those costs. REW's three-second check establishes that brief checks are practical in its workflow; it does not show that bursts would beat JTS's already-continuous loop.

Settling is not simply agreement between two adjacent numbers. A slowly changing response can differ little between adjacent windows while still being well away from its asymptote. Existing JTS tests already model that case. A faster estimator needs evidence that it ignores old buffered samples and waits through relevant room/device response. Confirmation should include actual input-setting readback and a fresh qualifying acoustic window; it must not be a repeated read of the same cached observation.

## Level definition and calibration

The existing result means the median of block RMS dBFS observations within 0.5-second windows, converted with a scalar microphone sensitivity. The controller normally requires two agreeing windows for a settled reading and two qualifying readings before banking. Its meter includes background across the microphone input; the declared stimulus band is not a measured-input bandpass. No A/C weighting or frequency-response calibration curve is applied by this level meter. It is not a standards-compliant sound-level meter and it is not an integrating LZeq measurement. [^12]

The new receipt explicitly describes that statistic, window, unweighted input scope, declared probe band or unknown, absent frequency-response correction, and unverified capture gain. It discloses existing uncertainty rather than granting calibration validity. Peak input clipping, the commissioning SPL stop, and acoustic target acceptance remain distinct observations. Maximum reported block-RMS SPL is neither an instantaneous pressure peak nor proof of available driver/amplifier headroom.

For a future normal listening-position acquisition, the proposed precise quantity is calibrated, unweighted equivalent continuous SPL over a one-second accepted window, with a declared analysis band matching the probe. Numerically, it is `10 log10(mean(p_B²) / (20 µPa)²)`, where `p_B` is calibrated pressure in the declared band and the mean spans that one-second window. Changing the band or statistic changes the meaning of the level, so a banked value under the current metric cannot simply be relabelled as this proposed metric. For the initial 75–80 dB request that would be 77.5 dB ±2.5 dB, evaluated on an explicitly identified probe at the anchor seat. One second is a proposed initial window to validate, not a measured optimal choice. The response-calibration curve and verified input-gain reference must participate before that quantity is called calibrated. Broadband calibrated block levels should still feed the existing commissioning stop independently of a narrower acquisition band.

A 500–2000 Hz probe is a defensible starting candidate for a summed speaker check because it avoids emphasizing sub-bass room noise and room modes; it is not valid for every isolated driver. A bass-only probe must intersect the actual admitted driver/program band and the question being asked. The commercial 30–80 Hz example is not permission to drive a JTS woofer below its declared floor. Near-field driver, passive-radiator, distortion and deliberate level-series measurements need their own admissible probe/level definition. The seat target must never silently become a universal target at every microphone position.

JTS's sensitivity parser records sensitivity, analog-gain metadata and serial identity. A scalar sensitivity only gives correct absolute SPL at the capture gain for which it was specified. Direct ALSA access does not itself prove that a microphone's hardware gain or AGC is fixed. Microphone documentation reinforces the importance of individual calibration files, orientation and controllable input gain. [^13][^14] An incorrect calibration or attenuated input can make the software under-read the physical SPL and its stop; it is not merely a display discrepancy.

DSP compression creates another ambiguity: an extra dB at Main need not produce another dB at the microphone. A response mismatch can indicate a limiter, driver compression, AGC, a transient, or changed microphone position. Freeze and record known DSP/capture conditions; avoid correcting indefinitely when the slope is not supported. During a declared compression or distortion study, this changing slope is the object of measurement. Automatic normalization of each rung would erase it.

## Shared ownership and evidence

| Existing owner | Required responsibility in the complete system |
|---|---|
| `MeasureSpec` and experiment definition | Declare automatic, deliberate quiet, or level-series intent; specify purpose, target metric, anchor, decision bands and allowed changes. Quiet/unusual levels require a reason. |
| `seat_level_ramp` and reference | Calculate and verify operating level. A saved reference proposes a starting setting and never substitutes for current evidence. Reuse this controller, with orchestration adapted to an already-open hold. |
| `SessionVolumePlan`, its door and volume claim | Sole authority to apply, hold, prove and restore Main; preserve durable cleanup and interruption handling. Do not nest a second plan or add another fader writer. |
| Shared `quality`, `quality_model`, `snr_policy` and program analysis | Report integrity, actual/unknown levels and band-specific decision support. Reuse existing model thresholds and same-domain spectral integration; distinguish capture corruption from intentional acoustic nonlinearity. |
| `TuningSession` and existing capture/recovery machinery | Coordinate current verification, playback/capture, retention and bounded retries. Converge UI-only policy into this path rather than adding a second loop. |
| CLI, UI and LLM tools | Submit the experiment and render the shared result. They must not manage separate leveling loops, hidden microphone prerequisites or quality diagnoses. |

“Same input” needs Main, waveform identity, stimulus amplitude and per-segment gains, active signal path, relevant DSP/loudness controls, capture gain/device and calibration context. A waveform hash alone does not identify separately applied gains. Readbacks and captured evidence should agree with the admitted conditions. A graph fingerprint change may be the named candidate intervention; it should not automatically invalidate a planned candidate comparison. An unplanned gain, graph, device or calibration change does require fresh evidence.

At group start, verify the anchor setup, acquire once and hold across uninterrupted rounds. An intended move to another position does not authorize a separate normalization there. Likewise, candidate changes should preserve input conditions so differences survive into the comparison. A changed input creates a new evidence epoch; the runner must identify and repeat the affected matched set instead of silently combining unlike takes. Earlier recordings, settings and reasons remain available.

The recording result needs separate answers to separate questions: were intact bytes captured, what actually played, what level was observed under which metric, and which bands support which decisions? Existing SNR policy already distinguishes magnitude and alignment requirements and unavailable/out-of-band support. Do not collapse that into a single “captured” or “passed” label. Matching ambient and active data must use the same domain and scaling, accounting for windowing and the actual emitted band. A full-band noise number cannot establish that a 25 Hz correction is justified.

| Condition | Required recovery behavior |
|---|---|
| Transient room noise | Retain the attempt, recheck background, retry at unchanged input under the existing bounded budget. |
| Persistently inadequate decision-band SNR | Report affected bands and shortfall; permit a new admitted excitation only if intent allows it, then reacquire and repeat the matched set. |
| Recorder clipping, dropped/invalid samples or identity mismatch | Report capture integrity failure, not speaker distortion. Correct the recording condition and retain all attempts. Any changed input/calibration requires fresh evidence. |
| Deliberate quiet experiment | Keep the requested level, state its purpose, and disclose limited bands. Do not quietly turn it into the normal seat target. |
| Deliberate volume series | Hold the declared series and reference conditions. Do not normalize each rung or back off an observed compression effect as though it were a capture fault. |
| Completion, exception or interruption | Stop the stimulus and use the existing shielded restoration path. Report actual restoration and any physical conditions that must be re-established. |

Audyssey's historical manual documents separate noisy-room errors and inadequate-SNR retries that can increase the test level up to twice per channel. [^4] (p.20) That is published product behavior, not the selected JTS policy. JTS's requested transient recovery explicitly forbids raising playback to overpower a disturbance. Retry scope must follow the experiment, and the existing admission/refusal registry must remain the single budget owner.

## Measured software baseline and implemented changes

Before modifications, the focused leveler, measurement CLI, session-volume and band-SNR suites passed 296 tests in 12.05 seconds. A temporary deterministic harness executed the leveler from the audited Git object and the instrumented implementation against the same existing synthetic microphone/clock fixtures. Target was 77.5 ±2.5 dB SPL, Main started at −50 dB with a −6 dB ceiling, and the simulated commissioning stop was 85 dB. These are test conditions, not JTS3's live declaration.

| Synthetic case | Before total s | After total s | After verification s from play request | Reported final SPL | Readings |
|---|---:|---:|---:|---:|---:|
| Linear response, 40 dB SPL at start | 10.170 | 10.170 | 8.550 | 77.496 | 8 |
| Linear response, 60 dB SPL at start | 6.720 | 6.720 | 5.400 | 77.496 | 5 |
| Already in target band | 3.150 | 3.150 | 2.100 | 77.502 | 2 |
| First-order response, 0.81 s time constant | 12.870 | 12.870 | 11.550 | 77.698 | 5 |
| First-order response, 3 s time constant | 22.400 | 22.400 | 21.050 | 78.870 | 7 |
| Mic does not observe output | 10.260 | 10.260 | Not verified | Refused | 8 |
| First sample above SPL stop | 1.100 | 1.100 | Not verified | Refused | 0 |

Every scenario retained exactly the same gain-command sequence, outcome, measured level, total virtual-clock time and household restoration. Each requested one playback start. Successful scenarios had no observed block-RMS excursion above the target band's upper edge; the slowest response reached a maximum of 78.938 dB SPL during the observed interval. The above-stop scenario observed 90 dB and refused after 0.050 simulated seconds from the playback request. These facts test software behavior only. They do not measure acoustic overshoot after teardown, JTS3 distortion/SNR, ALSA latency, playback dropouts or physical accuracy.

The new timing fields distinguish `operation_elapsed_s` (controller invocation through restoration; excludes CLI setup), `decision_after_play_request_s`, successful `verified_after_play_request_s`, and each observation's duration/completion offset. `playback_start_requests` counts controller requests, not DAC starts or detected audible interruptions. `max_meter_rms_db_spl` and `meter_excess_over_target_high_db` are room-inclusive calibrated-by-scalar block observations, not speaker-only excursions. Target error is reported only when a result converges. Refusals do not acquire a successful-verification timestamp.

Successful CLI output now carries the same `ramp` receipt that callers of the shared function receive. The shared quality assessor emits a failing `capture_nonfinite` finding with a sample count for NaN/infinity and suppresses misleading low-level warnings for invalid statistics; nonfinite samples do not count as recorder clipping. Comparison context now carries `loudness_volume_db`: known differences are incompatible for matched input conditions and missing values stay unknown. No new threshold, volume writer, leveler, fallback or retry budget is introduced.

## Live validation boundary

The target is JTS3 at `http://jts3.local/sound/speaker/`. The reported hardware is a Pi-connected microphone, a fixed-hardware-gain TPA3255 board on a 36 V supply, an E150HE-44 woofer and EPIQUE E180HE-PR passive radiator. None of those was confirmed from a live declaration during this work. Generic README hardware and test fixtures are not substitutes. Microphone model/serial/input gain, amplifier board gain, driver wiring/impedance, passive-radiator added mass, DSP operating state and available mechanical/electrical headroom remain unverified or unknown.

Before any future playback, read the actual unit identity, declaration, applied SHA, microphone/calibration state and shared busy/measurement ownership. An unreachable host is not an idle host. Acquire the existing measurement ownership rather than interfering with another operator. Use `scripts/pi-run-diagnostic.sh` for bounded diagnostics and only `scripts/deploy-to-pi.sh` for deployment, keeping its identity/direction guards. Verify the installed SHA and actual speaker UI after deployment; an edited checkout is not a deployed runtime.

The live before/after dataset must retain probe bytes/hash/band/peak/RMS, Main and DSP readbacks, calibration and capture-gain evidence, ambient/active waveforms, actual acoustic onset and interruptions, time-to-verified-level, target error, observed maximum and restoration. Separate setup, onset, settling, correction and teardown costs. Compare cold and stale-reference starts against current verified starts under otherwise matched conditions; use repeat counts sufficient to characterize ordinary variation rather than report one favorable run.

End-to-end acceptance remains: ordinary measurement requests acquire fresh level; explicit quiet requests retain their intent; matched candidates/positions preserve input; series remain unnormalized; changed conditions cause appropriate reacquisition and matched-set repetition; transient retries keep input fixed and terminate at the existing budget; capture faults differ from intentional speaker nonlinearity; unsupported bands are disclosed; all terminal paths restore. Physical tests must stay within the actual declared driver limits, nonpositive Main and commissioning stop. A 75–80 dB observation is an operating point, never evidence of spare amplifier or excursion capability.

## Sources

[^1]:
    REW. [Check Levels](https://www.roomeqwizard.com/help/help_en-GB/html/measurementlevel.html). Undated official help. Calibration procedure and input/output distinction.
[^2]:
    REW. [Making Measurements](https://mail.roomeqwizard.com/help/help_en-GB/html/makingmeasurements.html). Undated official help, “Check Levels,” Sweep Level and measurement-length sections. Published three-second check and fixed-level comparisons. Longer sweeps may improve SNR but separate capture/playback clocks require care; that trade-off must be measured on the Pi.
[^3]:
    Genelec. [How does the Standard Loudness SPL Calibration in GLM actually work?](https://support.genelec.com/hc/en-us/articles/21060845571090-How-does-the-Standard-Loudness-SPL-Calibration-in-GLM-actually-work). Updated 2024-11-26. Internal noise calibration and source-level dependence.
[^4]:
    Audyssey. [MultEQ-X User Guide, v1.0](https://audyssey.com/MultEQ-X%20User%20Guide.pdf). 2021, printed pp.17 and 20. Subwoofer display/acquisition and noise/SNR error behavior. Historical version; not assumed current.
[^5]:
    miniDSP. [Tide16: Configure Dirac Live](https://docs.minidsp.com/product-manuals/tide16/dirac-live/configure-dirac-live.html). Undated official product manual. Mic/output setup and background-noise guidance.
[^6]:
    miniDSP. [SHD AutoEQ: First steps](https://docs.minidsp.com/product-manuals/shd/autoeq/first-steps.html). Undated official product manual. Manual gain setup and shared noise/sweep frequency selection.
[^7]:
    Lyngdorf Audio. [RoomPerfect support](https://lyngdorf.steinwaylyngdorf.com/support-roomperfect/). Undated manufacturer documentation. Dedicated low/high-frequency signals and spatial method.
[^8]:
    REW. [Signal Generator](https://mail.roomeqwizard.com/betahelp/help/html/siggen.html). Undated official beta help, pink-noise and periodic-noise sections. Signal bands, spectral/crest-factor design and synchronization constraints.
[^9]:
    Angelo Farina. [Simultaneous Measurement of Impulse Response and Distortion with a Swept-Sine Technique](https://aes.org/publications/elibrary-page/?id=10211). AES 108th Convention, 2000, paper 5093. Accessible abstract; full text was not used.
[^10]:
    Guy-Bart Stan, Jean-Jacques Embrechts and Dominique Archambeau. [Comparison of Different Impulse Response Measurement Techniques](https://orbi.uliege.be/handle/2268/34825). JAES 50(4), 2002, pp.249–262; [author manuscript](https://orbi.uliege.be/bitstream/2268/34825/2/Stan_Embrechts_Archambeau_JAES_2002.pdf). Noise/nonlinearity dependence of measurement methods.
[^11]:
    ALSA project. [PCM interface](https://www.alsa-project.org/alsa-doc/alsa-lib/group___p_c_m.html), `snd_pcm_delay`; [PCM overview](https://www.alsa-project.org/alsa-doc/alsa-lib/pcm.html). Undated official library documentation. ADC/DAC delay and buffer/period semantics.
[^12]:
    REW. [SPL Meter](https://www.roomeqwizard.com/help/help_en-GB/html/splmeter.html). Undated official help. Integrating level, time weighting, frequency weighting and microphone calibration.
[^13]:
    miniDSP. [UMIK-2 User Manual](https://www.minidsp.com/images/documents/miniDSP%20UMIK-2-User%20Manual.pdf). Official manufacturer manual. Calibration, input gain and orientation. This does not establish that JTS3 has a UMIK-2.
[^14]:
    Dirac. [What microphone is required/recommended for Dirac Live?](https://helpdesk.dirac.com/en/faq/What-microphone-is-required-recommended-for-Dirac-Live-9a3b). Official support. Calibrated omnidirectional measurement microphone requirements; no JTS3 device inference.
