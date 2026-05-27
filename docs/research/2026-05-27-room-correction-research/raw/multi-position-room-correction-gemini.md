# **Rigorous Analysis of Multi-Position Room Correction Confidence for the JTS Smart Speaker System**

## **Executive Summary**

The ongoing development of the JTS smart speaker—powered by a Raspberry Pi 5 and the versatile CamillaDSP audio processing engine—represents a significant advancement in open-source audio engineering. The current JTS room-correction wizard successfully accomplishes the fundamental requirements of digital room correction (DRC): it emits an exponential sine sweep, deconvolves the impulse response, executes spatial averaging, and generates conservative parametric equalization (PEQ) filters.1 The critical evolutionary step for the JTS project is the implementation of a deterministic "measurement confidence" engine. This engine must evaluate spatial variance across multiple listening positions to autonomously determine the acoustic validity, safety, and necessity of proposed equalization filters, acting as a mathematical gatekeeper before any corrections are applied to the CamillaDSP pipeline.  
The acoustic behavior of small rooms is heavily segmented by the Schroeder frequency, transitioning from distinct modal resonances at low frequencies to dense, chaotic boundary reflections at higher frequencies.3 Because traditional single-point measurements cannot mathematically differentiate between a global room mode (which requires attenuation) and a localized cancellation null (which must never be boosted), multi-position spatial averaging is universally recognized within the acoustics literature as an absolute prerequisite for robust, high-fidelity room correction.5 Without spatial data, automated equalization systems risk degrading the time-domain performance of the loudspeaker and introducing severe perceptual artifacts.7  
This exhaustive research report establishes the theoretical frameworks, deterministic algorithms, and practical thresholds necessary to build a first-class confidence and variance reporting system for JTS. The analysis addresses the mathematical combinations of multiple spatial measurements, the extraction of minimum phase versus non-minimum phase acoustic features, and the formulation of a persistent data schema designed to facilitate future analysis by Large Language Models (LLMs). By evaluating the confirmed operational mechanics of industry-leading proprietary systems—including Audyssey, Dirac Live, Lyngdorf RoomPerfect, Trinnov, and Acourate—this report synthesizes a deterministic framework categorized by High Confidence, Medium Confidence, and Unresolved acoustic states. The resulting architecture ensures that JTS remains scientifically rigorous, acoustically conservative, and highly transparent to the end user.

## **The Acoustical Literature and Prior Art on Spatial Measurement**

To engineer a deterministic confidence engine, the underlying software must interpret acoustic data in strict accordance with the physical realities of sound propagation in enclosed spaces. The acoustics literature provides clear mandates regarding how room responses should be measured, analyzed, and corrected.  
The steady-state amplitude response measured at listening locations is a widely accepted indicator of sound quality, yet the literature definitively shows that correcting based on a single measurement point is deeply flawed.9 Researchers such as Floyd Toole, Sean Olive, and Todd Welti have extensively documented that spatial averaging adds vital acoustical information, whereas spectral averaging (such as standard 1/3-octave smoothing) permanently destroys information.5 Peaks in the frequency response that appear in a single on-axis measurement and persist through multiple off-axis and spatial average measurements are confirmed to be global resonances.5 Conversely, single-point measurements capture highly localized acoustic effects, such as sharp gain maxima, minima, and comb filtering caused by immediate boundary reflections.10 Spatial averaging prevents these local phenomena from dominating the measurement, allowing the equalization system to target and focus on essential, shared acoustic features rather than over-correcting localized anomalies.10  
The behavior of sound in a domestic listening environment is bifurcated by the Schroeder frequency, often referred to as the transition frequency.3 Below the Schroeder frequency, which typically resides between 200 Hz and 300 Hz in standard living spaces, the room acts as a complex resonant cavity.3 Sound energy in this regime is dominated by standing waves, or room modes, which cause severe peaks and nulls in the frequency response, often varying by as much as 15 to 20 dB.12 Because the wavelengths at these frequencies are exceptionally long (for example, a 30 Hz wave is approximately 11.4 meters long), these modal peaks represent global accumulations of acoustic energy that persist across wide spatial areas. Furthermore, room modes are generally minimum-phase phenomena, meaning they are highly responsive to amplitude equalization.14  
Above the Schroeder frequency, the sound field transitions into ray acoustics, becoming dense with overlapping reflections and boundary interference.4 Anomalies in this high-frequency regime are highly localized; moving a measurement microphone merely a few centimeters will radically alter the interference pattern, leading to dramatic shifts in the measured phase and amplitude.11 Consequently, applying narrow, high-Q equalization above the Schroeder frequency based on a small spatial sample is universally condemned in the literature, as correcting a narrow dip at one ear will inevitably create an artificial peak at the other ear.15  
A high-confidence digital room correction system must, therefore, apply fundamentally different correction philosophies across these two regimes. Below the Schroeder frequency, assertive correction of modal peaks operates with high confidence. Above the Schroeder frequency, correction requires broad, low-Q adjustments based purely on spatially consistent trends, leaning heavily on the natural directivity and anechoic response of the loudspeaker itself.15

## **Combining Multiple Seat and Listening-Position Measurements**

When a JTS user captures measurements across multiple seats in their environment, the software must combine these complex impulse responses or frequency responses into a single actionable target curve. The mathematical method chosen for this combination dictates the safety and accuracy of the resulting equalization.  
Arithmetic decibel (dB) averaging is a common but mathematically flawed approach. In this method, the magnitude values in dB from each measurement position are simply summed and divided by the number of positions. This approach gives equal mathematical weight to extreme peaks and extreme dips. For example, a \+20 dB room mode peak and a \-20 dB cancellation null will average to 0 dB, completely masking the massive variance in acoustic energy.19 The resulting average implies a flat response where none exists, blinding the DSP to severe acoustic problems.  
Linear power averaging, also known as Root Mean Square (RMS) averaging, is the scientifically superior method for deriving an equalization target.19 In this process, the dB values from each spatial position are converted to linear magnitudes, squared, summed, divided by the number of measurements, and then the square root of the result is converted back to dB.19 Because this method averages the actual acoustic power rather than the logarithmic representation, high-energy peaks are weighted much more heavily than deep nulls.19 Given that the primary objective of conservative room correction is to attenuate resonant peaks without attempting to dangerously boost non-minimum phase nulls, RMS averaging natively guides the algorithm toward safer, highly confident corrections.15  
Vector averaging, or complex averaging, combines both the magnitude and the phase (the real and imaginary parts of the Fast Fourier Transform) before calculating the final magnitude response.19 While theoretically powerful for maintaining time-domain integrity, vector averaging introduces severe complications when applied to spatially diverse measurements. Unless microphone positions are aligned with millimeter precision, the phase differences between positions at higher frequencies will result in destructive interference. Vector averaging these out-of-phase signals causes severe comb filtering and artificial high-frequency roll-off in the averaged response.20 Therefore, vector averaging must be strictly avoided for calculating the global target magnitude curve.20 It should only be utilized in highly specific sub-routines, such as time-aligning multiple subwoofers at very low frequencies (typically below 80 Hz), where the wavelengths are long enough that minor microphone displacements do not induce phase cancellation.21  
The physical method of data collection also deeply impacts confidence calculation. The Moving Microphone Measurement (MMM) technique utilizes uncorrelated pink noise and a Real-Time Analyzer (RTA) with infinite averaging while the user slowly sweeps the microphone through a listening volume in a randomized pattern.22 The literature demonstrates that a proper MMM scan over a continuous 60-second period provides a highly reliable spatial average of the power response that correlates perfectly to human perception, effectively bypassing the need for complex multi-point deconvolutions.8 However, MMM possesses critical limitations for a deterministic confidence engine. It permanently destroys all phase and impulse response timing data, making it impossible to calculate excess group delay.24 Furthermore, because MMM produces a single averaged curve, the DSP is incapable of calculating spatial variance.25 The software cannot mathematically determine if a 6 dB peak was present evenly across the entire couch, or if it was an 18 dB peak localized entirely to one specific corner.  
To build a deterministic confidence report, the JTS software requires discrete fixed measurements. The ideal realistic upper bound for a typical consumer is between 3 and 5 fixed positions. This provides a sufficient statistical baseline to calculate standard deviations across the listening area without inflicting user fatigue.26 While a highly dense "rich point measurement map" (e.g., 20 to 50 positions) allows for granular mapping of 3D wave propagation and precise localization of high-frequency reflections, it provides rapidly diminishing returns for the primary goal of low-frequency modal correction. Because modal wavelengths are immense, 5 carefully placed measurements across a primary seating area capture the global structural bounds of the low-frequency acoustic environment just as effectively as 50 points.13 Therefore, JTS should mandate a minimum of 3 and a default of 5 discrete sweeps, capturing the exact magnitude, phase, timing, and variance data needed to compute confidence matrices without overwhelming the user.

## **Distinguishing Acoustic Features via Software**

To generate a sophisticated confidence report, the software must autonomously categorize the acoustic features extracted from the discrete measurements. This requires translating acoustic physics into deterministic code. The most critical distinction the software must make is between minimum phase features and non-minimum phase features.  
In a minimum phase system, amplitude and phase are strictly coupled by the Hilbert transform.28 Room modes, which manifest as large peaks in the frequency response, generally exhibit minimum phase behavior. Applying a parametric equalization cut to a minimum phase peak not only corrects the amplitude anomaly but simultaneously reduces the time-domain ringing and decay of that resonance.14 These features yield High Confidence for automated correction. Conversely, acoustic nulls caused by Speaker-Boundary Interference Response (SBIR) or local reflections are non-minimum phase.30 In these scenarios, the direct sound from the loudspeaker and the reflected sound from a nearby boundary arrive at the listener out of phase, causing a deep cancellation.30 Attempting to boost a non-minimum phase null with EQ simply injects more acoustic energy into both the direct and reflected paths; the cancellation remains intact, but the amplifier is driven toward clipping, distortion increases, and the time-domain response is severely degraded.15 These features dictate Zero Confidence for boosting.  
Software can mathematically distinguish these regions by calculating the Excess Group Delay (EGD). Group delay is the negative derivative of the phase with respect to frequency, representing the time delay of various frequency components.31 The software calculates the minimum phase response via the Hilbert transform of the measured magnitude response.28 By subtracting this theoretical minimum group delay from the actual measured group delay, the software derives the EGD.15 Regions where the EGD is flat and hovers near zero indicate minimum phase behavior, confirming that peaks in this region are correctable modes.15 Regions characterized by sharp, erratic spikes in the EGD indicate non-minimum phase cancellations, warning the software that equalization is mathematically futile.15

### **High Confidence: Repeatable Modal Peaks**

A repeatable modal peak is the primary target for JTS room correction. The software identifies this feature through three distinct metrics. First, it requires high spatial coherence; the peak must appear at the same frequency (within a fractional octave tolerance) across all measured positions. Second, it requires low spatial variance; the standard deviation of the magnitude at this frequency across the ![][image1] positions must be statistically low, indicating the peak is a global room issue rather than a local boundary effect.22 Third, the feature must exhibit minimum phase behavior, confirmed by a flat EGD curve.15 When these conditions are met, the software assigns High Confidence and designs a precise PEQ cut to flatten the response, actively reducing room decay.

### **Medium Confidence: Seat-Local Peaks**

A seat-local peak is caused by a listener's proximity to a specific boundary, such as a rear wall, or a localized reflection pattern. The software identifies this feature by observing high spatial variance.13 For example, the peak may register at \+9 dB in position 1, but only \+1 dB in positions 2 and 3\. The standard deviation across the positions is high.13 The software assigns Medium Confidence to this feature. Because standard RMS averaging dilutes the mathematical severity of the peak across the aggregate curve, the algorithm must act conservatively. It restricts the applied PEQ cut to a fraction of the peak's localized amplitude to avoid over-correcting and hollowing out the bass response in the seats where the peak does not physically exist.10

### **Unresolved / Zero Confidence: Narrow Nulls and Cancellations**

Nulls are the primary danger zone in automated digital room correction.33 The software detects them by identifying sharp, high-Q dips in the magnitude response accompanied by massive spikes in the Excess Group Delay precisely at the null frequency.15 Furthermore, these cancellations usually exhibit high spatial variance, shifting rapidly in frequency as the microphone is moved across the seating area.34 The software must assign Unresolved or Zero Confidence to these features. The algorithm must implement strict "dip limiting," ensuring that no positive gain PEQ filters are applied to these cancellations, protecting the amplifier and preserving the transient response of the loudspeaker.35

### **Artifacts and Hardware Failures**

The confidence engine must also protect against faulty data collection. Measurement artifacts such as HVAC rumble, traffic noise, or handling the microphone during a sweep severely corrupt the impulse response. The software detects this by analyzing the Signal-to-Noise Ratio (SNR), calculating the mathematical coherence between the known output stimulus and the recorded sweep.36 If coherence drops below an acceptable threshold, or if consecutive identical sample values at digital maximum (0 dBFS) indicate microphone clipping, the software assigns Zero Confidence and triggers a mandatory "Measure Again" prompt to the user.36 Similarly, if the software detects a massive, broad-spectrum deviation—such as a 15 dB drop in all high-frequency energy—it flags a placement or hardware warning. Equalization cannot fix a blown tweeter or an entirely occluded speaker without destroying system headroom.7

## **Deterministic Thresholds and Rationale**

To operate deterministically, the confidence engine requires hard mathematical thresholds. The following values are synthesized from acoustic literature and the configuration standards of leading open-source frameworks like DRC-FIR and Room EQ Wizard (REW).1

| Parameter | Threshold | Rationale | Confidence Level Mapping |
| :---- | :---- | :---- | :---- |
| **Minimum Positions** | 3 (Default: 5\) | A single position provides no spatial variance data. More than 5 yields diminishing returns for standard residential environments.26 | N/A |
| **Spatial Variance (Std. Dev ![][image2])** | **![][image3]** dB | The acoustic feature is highly consistent and stable across the entire listening area.22 | **High** |
| **Spatial Variance (Std. Dev ![][image2])** | **![][image4]** dB | The feature is present globally, but its severity varies depending on the specific seat.39 | **Medium** |
| **Spatial Variance (Std. Dev ![][image2])** | **![][image5]** dB | The feature is highly localized to a single seat, or it is a cancellation null shifting rapidly across the space.39 | **Low / Unresolved** |
| **Maximum Cut (Negative Gain)** | \-12.0 dB | Room modes can be exceptionally severe. Subtractive EQ removes energy and ringing without risking amplifier clipping or distortion.33 | **High** |
| **Maximum Boost (Positive Gain)** | \+3.0 dB to \+6.0 dB | Strictly caps boosting to prevent blowing drivers or clipping amplifiers. Severe boundary nulls physically cannot be filled by EQ.33 | **Low** |
| **Max Q Factor (Below ![][image6])** | **![][image7]** | Low-frequency modes are often narrow and require precise, high-Q cuts to eliminate ringing without affecting adjacent frequencies.14 | **High** |
| **Max Q Factor (Above ![][image6])** | **![][image8]** | High frequencies require gentle, broad "shelving" adjustments. Narrow, high-Q EQ above the Schroeder frequency is perceptually flawed and causes localized ringing.15 | **Medium** |
| **Minimum Peak Width** | **![][image9]** Octaves | Peaks narrower than 0.1 octaves are virtually inaudible to human hearing and are frequently artifacts of the FFT windowing.11 | **Low** |
| **Coherence / SNR Limit** | **![][image10]** dB SNR | A minimum acoustic signal above the ambient noise floor is required for accurate mathematical deconvolution of the impulse response. | **High** |

### **Single-Position Fallback Confidence**

The system must gracefully handle scenarios where a user refuses to take multiple measurements and proceeds with a single sweep. Because the algorithm cannot calculate spatial variance (![][image2]) when the number of positions equals one, it risks misinterpreting highly localized reflections as global room modes. If a single measurement is forced, the software must instantly downgrade its confidence parameters globally. Above 200 Hz, confidence drops to Low or Unresolved; the software absolutely must not apply narrow PEQ in the ray acoustic regime based on a single point.18 Below 200 Hz, confidence is downgraded to Medium. The software can still apply subtractive cuts to prominent peaks, assuming they are modes, but it must enforce aggressive dip-limiting (capping maximum boost at 0 dB) to guarantee it does not attempt to equalize a localized boundary null.33 The maximum allowed Q should also be constrained to prevent over-fitting a local, single-point artifact.

### **Mono vs. Stereo Configurations**

The form factor of the JTS speaker dictates distinct confidence evaluation strategies. For a single mono speaker (often placed near a wall or in a corner), asymmetrical room boundary loading will cause severe skewing of the response. Stereo imaging preservation is irrelevant in this context. The confidence engine should prioritize the raw power response via RMS averaging, aggressively applying subtractive PEQ to combat the inevitable boundary gain and corner-loaded room modes. For stereo pairs, JTS must measure the Left and Right channels independently. The confidence engine must then compare the spatial variance between the channels. If the room is roughly symmetric, the software should average the Left and Right target responses and apply identical correction filters to both speakers. This methodology, utilized by advanced systems like the Trinnov Optimizer, preserves the delicate inter-channel phase relationships and maintains accurate stereo imaging geometry.42 If the software detects massive asymmetry (e.g., Left speaker in a corner, Right speaker in an open hallway), it must abandon symmetric correction and apply independent L/R equalization to restore tonal balance.

## **Prior-Art Ecosystem Analysis: Best Practices**

An analysis of industry-leading proprietary and open-source DSP room correction systems reveals highly consistent methodologies regarding spatial averaging, clustering, and gain limiting. JTS can emulate the mathematical principles underlying these systems without infringing on proprietary algorithms.

| System | Spatial Averaging Logic | Feature Extraction & Confidence Indication | Key Strengths for JTS to Emulate |
| :---- | :---- | :---- | :---- |
| **Audyssey MultEQ** | Fuzzy c-means clustering algorithm.45 | Weights measurements by pattern similarity rather than simple averaging; prevents outlier seats from destroying the global average.46 | Utilizing variance-weighted averaging to mathematically group similar seating positions.48 |
| **Dirac Live** | Mixed-phase impulse response processing.44 | Analyzes spatial stability and variability across the listening area to restrict phase correction to highly correlated regions.49 | Limiting the severity of correction based on the spatial consistency of the measured impulse response.6 |
| **Lyngdorf RoomPerfect** | Distinguishes between Focus (Primary) and Global (Random) spatial measurements.26 | Calculates a "RoomKnowledge" index that dictates when enough 3D spatial data has been captured to safely formulate gain limits.27 | Establishing strict upper and lower gain limits derived directly from the 3D room power response.51 |
| **Trinnov Optimizer** | Multi-point weighted matrix calculations.37 | Computes optimal phase and magnitude per position, allowing users to assign confidence weights to specific seats.52 | Averaging the Left and Right target responses to perfectly preserve stereo imaging and phase alignment.42 |
| **Genelec GLM** | Multi-point RMS averaging.53 | Emphasizes subtractive EQ and broad shelving filters over positive boosts.53 | Strict adherence to cuts-first PEQ logic, completely ignoring narrow non-minimum phase nulls.53 |
| **HouseCurve** | Magnitude RMS averaging.36 | Implements "coherence blanking," explicitly ignoring frequency data that falls below a predetermined SNR threshold.36 | Visualizing "blanked" zones where the algorithm determines EQ is mathematically unsafe to apply.55 |
| **Acourate / DRC-FIR** | Frequency Dependent Windowing (FDW).35 | Utilizes explicit peak limiting (PLMaxGain) and dip limiting (DLMinGain) algorithms to prevent over-correction of low acoustic energy zones.35 | Exposing explicit, deterministic variables for maximum gain, minimum gain, and dip-limiting exponents.35 |

Audyssey MultEQ revolutionized multi-point correction by moving away from simple arithmetic averages. It utilizes a fuzzy c-means clustering algorithm, which evaluates the acoustic similarities between impulse responses using mathematical pattern recognition.45 If one microphone placement yields an anomalous measurement, the clustering algorithm assigns it a lower weight, preventing it from skewing the equalization of the primary seating area.48  
Dirac Live takes a different approach, focusing heavily on mixed-phase correction.49 However, Dirac specifically constrains its phase and amplitude corrections by analyzing spatial variability. If a non-minimum phase anomaly is detected that shifts wildly across the measurement positions, Dirac recognizes the lack of spatial stability and intelligently restricts correction in that zone to prevent pre-ringing artifacts.6  
Lyngdorf's RoomPerfect system operates on a unique paradigm of "RoomKnowledge." It measures the primary listening focus position and then mandates a series of random room measurements.27 The system continuously calculates the global 3D energy of the room, updating the RoomKnowledge percentage. Once it crosses 90%, the software determines it has enough spatial data to confidently establish upper and lower gain limits for the correction filters.26  
DRC-FIR, a highly respected open-source framework, provides the most transparent look at gain management. It uses explicit algorithms for Peak Limiting (PLMaxGain) and Dip Limiting (DLMinGain).35 Dip limiting is specifically designed to restrict the boost applied to sharp response dips, preventing the correction filters from dangerously over-amplifying areas with low acoustic energy.35

## **Proposed Algorithms and Pseudocode**

To implement this deterministic logic within the JTS Python and CamillaDSP environment 1, the software must execute distinct algorithmic steps to compute variance-weighted averages, evaluate Excess Group Delay, and map these metrics to confidence gates.

### **Variance-Weighted Target Generation**

Drawing inspiration from Audyssey's clustering approach, JTS should implement a variance-weighted average to prevent an anomalous, low-confidence measurement (such as a microphone accidentally placed inches from a reflective wall) from corrupting the global target.

Python  
import numpy as np

def calculate\_variance\_weighted\_average(measurements\_db):  
    """  
    measurements\_db: List of arrays containing magnitude responses in dB  
    """  
    \# 1\. Establish a median baseline resistant to outliers  
    median\_response \= np.median(measurements\_db, axis=0)  
    weights \=  
      
    \# 2\. Calculate Mean Squared Error (MSE) for each position against the median  
    for m in measurements\_db:  
        mse \= np.mean((m \- median\_response)\*\*2)  
        \# Assign higher weight to measurements that match the spatial consensus  
        weight \= 1.0 / (mse \+ 1e-6)   
        weights.append(weight)  
          
    \# 3\. Normalize weights to sum to 1.0  
    weights \= np.array(weights) / sum(weights)  
      
    \# 4\. Convert dB to linear power for accurate acoustic energy averaging  
    linear\_power \= np.array(\[10 \*\* (m / 10.0) for m in measurements\_db\])  
      
    \# 5\. Calculate weighted average of linear power and convert back to dB  
    weighted\_linear \= np.average(linear\_power, axis=0, weights=weights)  
    weighted\_avg\_db \= 10 \* np.log10(weighted\_linear)  
      
    return weighted\_avg\_db, weights

### **Spatial Variance and Confidence Calculation**

The core decision engine iterates through the frequency bins, analyzing the spatial standard deviation and the Excess Group Delay to deterministically assign confidence levels and establish absolute gain limits for the CamillaDSP PEQ generation.

Python  
def calculate\_confidence\_and\_limits(measurements\_db, freq\_bins, phase\_data):  
    num\_positions \= len(measurements\_db)  
      
    \# 1\. Calculate spatial standard deviation (variance in dB)  
    spatial\_variance \= np.std(measurements\_db, axis=0)  
      
    \# 2\. Derive Excess Group Delay (EGD)   
    \# EGD \= Measured Group Delay \- Hilbert\_Transform(Minimum Group Delay)  
    egd\_array \= calculate\_excess\_group\_delay(measurements\_db, phase\_data)  
      
    confidence\_report \=  
    allowed\_eq\_limits \=  
      
    \# 3\. Evaluate confidence per-frequency bin  
    for i, freq in enumerate(freq\_bins):  
        variance \= spatial\_variance\[i\]  
        egd \= egd\_array\[i\]  
          
        \# Define Acoustic Regime based on estimated Schroeder Frequency  
        is\_modal\_region \= freq \< 300   
          
        \# Fallback for single-position measurement  
        if num\_positions \== 1:  
            confidence \= "MEDIUM" if is\_modal\_region else "LOW"  
            max\_cut \= \-6.0  
            max\_boost \= 0.0 \# Strict dip-limiting enforced  
              
        \# Standard Multi-Position Confidence Gates  
        elif variance \< 3.0 and egd \< 2.0:   
            \# High stability, minimum phase behavior confirmed  
            confidence \= "HIGH"  
            max\_cut \= \-12.0  
            max\_boost \= 3.0 if is\_modal\_region else 0.0  
              
        elif variance \< 6.0 and egd \< 5.0:   
            \# Moderate spatial variance  
            confidence \= "MEDIUM"  
            max\_cut \= \-6.0  
            max\_boost \= 0.0  
              
        else:   
            \# High variance or severe non-minimum phase spike (Cancellation Null)  
            confidence \= "LOW"  
            max\_cut \= \-3.0 \# Allow only gentle smoothing  
            max\_boost \= 0.0 \# NEVER boost unresolved nulls  
              
        confidence\_report.append(confidence)  
        allowed\_eq\_limits.append((max\_cut, max\_boost))  
          
    return confidence\_report, allowed\_eq\_limits

## **Structure of the Correction Confidence Report**

The user-facing output—whether rendered in the JTS web interface or ingested by a future LLM assistant—must translate this dense deterministic matrix into actionable, readable insights. It acts as the final gatekeeper before filters are hot-loaded into the CamillaDSP engine.  
The report must begin with a Measurement Integrity Audit. This section evaluates the raw capture data and issues hard stops ("Measure Again" triggers) if the Signal-to-Noise Ratio falls below 15 dB, if digital clipping is detected in the impulse response, or if the spatial variance exceeds physical realities (indicating the microphone was dropped or impacted during the sweep).36  
Following the audit, the report provides a Per-Band Confidence Breakdown. For the sub-bass region (20-80 Hz), it should translate High Confidence data into plain language: "High Confidence. Identified two global room modes. Safely applying subtractive EQ to reduce ringing." In the mid-bass region, it must explain Medium or Low confidence states: "Medium Confidence. Identified a deep null at 115 Hz. High spatial variance and excess group delay indicate a boundary cancellation. Action: Ignored to protect amplifier headroom."  
The report should visually display the "Per-Filter Rationale," providing a table that lists each proposed PEQ filter alongside its specific Spatial Variance, EGD, and Confidence Gate status, mathematically justifying why the algorithm selected specific Gain and Q parameters.  
Finally, the report should expose "Strategy Gates" to the user, allowing them to dictate how aggressively the confidence engine operates:

* **Safe Strategy:** The system only corrects High Confidence features (variance \< 3dB, minimum phase). Maximum cut is capped at \-6 dB, and no positive boosting is allowed under any circumstance.  
* **Balanced Strategy (Default):** The system corrects High and Medium confidence features. Maximum cut is allowed up to \-12 dB, and very conservative boosting (up to \+3 dB) is permitted below the Schroeder frequency. Strict dip-limiting is applied to all non-minimum phase nulls.  
* **Assertive Strategy:** Pushes the boundaries of Medium confidence, allowing up to \+6 dB of boost in the modal range. It may deploy Frequency Dependent Windowing (FDW) to attempt gentle, broad magnitude correction above the Schroeder frequency. This is only recommended for highly treated acoustic spaces.

## **Proposed Confidence Schema and Persistence**

To persist the measurement bundle for future analysis, re-evaluation, or ingestion by an AI assistant capable of explaining acoustic logic in natural language, JTS must define a robust, serialized JSON schema. This schema ensures every mathematical decision is permanently tied to the audio data.

JSON  
{  
  "jts\_measurement\_bundle\_version": "1.2",  
  "metadata": {  
    "timestamp": "2026-05-26T11:36:00Z",  
    "microphone\_model": "UMIK-1",  
    "calibration\_file\_applied": true,  
    "speaker\_configuration": "STEREO",  
    "positions\_measured": 5  
  },  
  "global\_metrics": {  
    "schroeder\_frequency\_estimate\_hz": 245.0,  
    "average\_snr\_db": 42.5,  
    "spatial\_similarity\_index": 0.82  
  },  
  "per\_position\_data":,  
  "feature\_extraction\_matrix":,  
  "camilladsp\_payload": {  
    "filters": {  
      "peq\_mode\_1": {"type": "Biquad", "parameters": {"type": "Peaking", "freq": 48.5, "q": 4.2, "gain": \-6.5}}  
    },  
    "pipeline":  
  }  
}

This schema provides total transparency. An LLM ingesting this JSON can instantly explain to a user: *"I see you have a deep null at 115 Hz. However, the spatial variance was 8.5 dB and the group delay was high, meaning this is a reflection cancellation. JTS safely ignored this to protect your speaker."*

## **Risks, Edge Cases, and Practical Recommendations**

A deterministic DSP system must be engineered to gracefully handle physical edge cases that fall outside standard acoustic assumptions.  
Extreme room asymmetry poses a significant risk to automated stereophonic correction. If a room is highly asymmetrical—for example, the Left speaker is loaded into a hard corner while the Right speaker is adjacent to an open hallway—the spatial variance between the two channels will be massive. The standard practice of averaging the Left and Right targets to preserve stereo imaging geometry 43 will fail in this environment, resulting in severe under-correction of the corner-loaded speaker. To mitigate this, the JTS confidence engine must calculate an Inter-Channel Similarity Index prior to generating filters.44 If the L and R responses correlate poorly, the system must abandon symmetric target generation and apply independent equalization to each channel, accepting a theoretical degradation in stereo phase coherence in exchange for the necessary restoration of tonal balance.  
Ambient noise overpowering a deep acoustic null presents a secondary mathematical risk. A severe 30 dB floor-bounce cancellation may push the recorded sweep signal entirely below the ambient noise floor of the room (such as HVAC rumble or traffic noise). In this scenario, the FFT deconvolution algorithm may falsely interpret the low-frequency noise floor as acoustic stimulus energy from the speaker, calculating a wildly inaccurate phase response. To mitigate this, the software must implement coherence blanking, similar to the HouseCurve application.36 By mathematically comparing the power of the clean stimulus sweep to the power of the recorded sweep in the frequency domain, the software can explicitly blank out or ignore any frequency bin where the coherence falls below a safe threshold, assigning it absolute Zero Confidence and preventing catastrophic EQ errors.36  
In conclusion, the successful implementation of a deterministic, multi-position room correction engine for the JTS smart speaker relies on abandoning simple arithmetic averages in favor of RMS power averaging, utilizing spatial variance as the primary metric for statistical confidence, and analyzing Excess Group Delay to strictly identify and avoid non-minimum phase nulls. By operating within this rigorous mathematical framework, JTS guarantees that its CamillaDSP engine receives only highly reliable, acoustically conservative PEQ filters, achieving state-of-the-art room equalization while remaining fully open-source and entirely transparent.

#### **Works cited**

1. GitHub \- HEnquist/camilladsp: A flexible cross-platform IIR and FIR engine for crossovers, room correction etc., accessed May 26, 2026, [https://github.com/HEnquist/camilladsp](https://github.com/HEnquist/camilladsp)  
2. CamillaDSP, accessed May 26, 2026, [https://henquist.github.io/](https://henquist.github.io/)  
3. Modes, room ratios, and the "Schroeder Frequency" or "Transition Frequency" \- Soundman2020 \- Studio Design Forum, accessed May 26, 2026, [https://www.digistar.cl/Forum/viewtopic.php?t=794](https://www.digistar.cl/Forum/viewtopic.php?t=794)  
4. Loudspeaker-Room Correction of Conference Rooms \- Diva-Portal.org, accessed May 26, 2026, [https://www.diva-portal.org/smash/get/diva2:1805002/FULLTEXT01.pdf](https://www.diva-portal.org/smash/get/diva2:1805002/FULLTEXT01.pdf)  
5. Audio \- science \- HARMAN International, accessed May 26, 2026, [https://www.harman.com/documents/audioscience\_0.pdf](https://www.harman.com/documents/audioscience_0.pdf)  
6. Multiple-point statistical room correction for audio reproduction: Minimum mean squared error correction filtering \- ResearchGate, accessed May 26, 2026, [https://www.researchgate.net/publication/24264782\_Multiple-point\_statistical\_room\_correction\_for\_audio\_reproduction\_Minimum\_mean\_squared\_error\_correction\_filtering](https://www.researchgate.net/publication/24264782_Multiple-point_statistical_room_correction_for_audio_reproduction_Minimum_mean_squared_error_correction_filtering)  
7. Room Correction: Benefits and Pitfalls \- JansZenAudio, accessed May 26, 2026, [https://janszenaudio.com/blogs/issues-in-audio/room-correction](https://janszenaudio.com/blogs/issues-in-audio/room-correction)  
8. Genelec GLM Review (Room EQ & Setup) | Page 10 | Audio Science Review (ASR) Forum, accessed May 26, 2026, [https://www.audiosciencereview.com/forum/index.php?threads/genelec-glm-review-room-eq-setup.26397/page-10](https://www.audiosciencereview.com/forum/index.php?threads/genelec-glm-review-room-eq-setup.26397/page-10)  
9. The Measurement and Calibration of Sound Reproducing Systems \- Linkwitz Lab, accessed May 26, 2026, [https://www.linkwitzlab.com/Toole-Room%20calibration.pdf](https://www.linkwitzlab.com/Toole-Room%20calibration.pdf)  
10. Spatial Stability of the Frequency Response Estimate and the ..., accessed May 26, 2026, [https://assets.ctfassets.net/4zjnzn055a4v/6nKDWR1VsWp2WkaAqdvdDT/09402de507362053495f5f53e8a86d87/AES\_141\_-\_Spatial\_Stability\_of\_the\_Frequency\_Response\_Estimate\_and\_the\_Benefit\_of\_Spatial\_Averaging.pdf](https://assets.ctfassets.net/4zjnzn055a4v/6nKDWR1VsWp2WkaAqdvdDT/09402de507362053495f5f53e8a86d87/AES_141_-_Spatial_Stability_of_the_Frequency_Response_Estimate_and_the_Benefit_of_Spatial_Averaging.pdf)  
11. Bass Trap Myths \- Ethan Winer, accessed May 26, 2026, [https://ethanwiner.com/basstrap\_myths.htm](https://ethanwiner.com/basstrap_myths.htm)  
12. Acoustic Measurements: Understanding Time and Frequency | Audio Science Review (ASR) Forum, accessed May 26, 2026, [https://www.audiosciencereview.com/forum/index.php?threads/acoustic-measurements-understanding-time-and-frequency.25/](https://www.audiosciencereview.com/forum/index.php?threads/acoustic-measurements-understanding-time-and-frequency.25/)  
13. A cluster centroid method for room response equalization at multiple locations, accessed May 26, 2026, [https://www.researchgate.net/publication/3927303\_A\_cluster\_centroid\_method\_for\_room\_response\_equalization\_at\_multiple\_locations](https://www.researchgate.net/publication/3927303_A_cluster_centroid_method_for_room_response_equalization_at_multiple_locations)  
14. Hard proof that equalization kills room modes \- Acoustic Frontiers LLC, accessed May 26, 2026, [https://acousticfrontiers.com/blogs/articles/hard-proof-that-equalization-kills-room-modes](https://acousticfrontiers.com/blogs/articles/hard-proof-that-equalization-kills-room-modes)  
15. Minimum Phase \- Room EQ Wizard, accessed May 26, 2026, [https://www.roomeqwizard.com/help/help\_en-GB/html/minimumphase.html](https://www.roomeqwizard.com/help/help_en-GB/html/minimumphase.html)  
16. Full Acoustic Room Impulse Response of a Small Smart Speaker | COMSOL Blog, accessed May 26, 2026, [https://www.comsol.com/blogs/full-acoustic-room-impulse-response-of-a-small-smart-speaker](https://www.comsol.com/blogs/full-acoustic-room-impulse-response-of-a-small-smart-speaker)  
17. Question about minimum phase resonance and corrective EQ \- DSP and Plugin Development Forum \- KVR Audio, accessed May 26, 2026, [https://www.kvraudio.com/forum/viewtopic.php?t=456229](https://www.kvraudio.com/forum/viewtopic.php?t=456229)  
18. Should we correct to Schroder, or full range? \- Audio Science Review (ASR) Forum, accessed May 26, 2026, [https://www.audiosciencereview.com/forum/index.php?threads/should-we-correct-to-schroder-or-full-range.46582/](https://www.audiosciencereview.com/forum/index.php?threads/should-we-correct-to-schroder-or-full-range.46582/)  
19. All SPL Graph \- Room EQ Wizard, accessed May 26, 2026, [https://www.roomeqwizard.com/help/help\_en-GB/html/graph\_allspl.html](https://www.roomeqwizard.com/help/help_en-GB/html/graph_allspl.html)  
20. Value of room correction? | Page 3 | Audio Science Review (ASR) Forum, accessed May 26, 2026, [https://www.audiosciencereview.com/forum/index.php?threads/value-of-room-correction.11852/page-3](https://www.audiosciencereview.com/forum/index.php?threads/value-of-room-correction.11852/page-3)  
21. Question on Averaging | AV NIRVANA, accessed May 26, 2026, [https://www.avnirvana.com/threads/question-on-averaging.7823/](https://www.avnirvana.com/threads/question-on-averaging.7823/)  
22. pink ? A method for spatial averaging of loudspeakers in-room ..., accessed May 26, 2026, [https://www.ohl.to/audio/downloads/MMM-moving-mic-measurement.pdf](https://www.ohl.to/audio/downloads/MMM-moving-mic-measurement.pdf)  
23. The moving microphone method (MMM) for Dummies using REW | Audio Science Review (ASR) Forum, accessed May 26, 2026, [https://www.audiosciencereview.com/forum/index.php?threads/the-moving-microphone-method-mmm-for-dummies-using-rew.51333/](https://www.audiosciencereview.com/forum/index.php?threads/the-moving-microphone-method-mmm-for-dummies-using-rew.51333/)  
24. Pink Noise Calibration – Audio \- Kvålsvoll Design AS, accessed May 26, 2026, [https://www.kvalsvoll.com/blog/2014/07/06/pink-noise-calibration/](https://www.kvalsvoll.com/blog/2014/07/06/pink-noise-calibration/)  
25. Room Correction Beta firmware for WiiM Pro, Pro Plus and Amp \- User Testing Experience | Page 7, accessed May 26, 2026, [https://forum.wiimhome.com/threads/room-correction-beta-firmware-for-wiim-pro-pro-plus-and-amp-user-testing-experience.3138/page-7](https://forum.wiimhome.com/threads/room-correction-beta-firmware-for-wiim-pro-pro-plus-and-amp-user-testing-experience.3138/page-7)  
26. RoomPerfect DSP room correction \- Audio Engineering Society, accessed May 26, 2026, [http://www.aes-media.org/sections/uk/meetings/AESUK\_lecture\_0611.pdf](http://www.aes-media.org/sections/uk/meetings/AESUK_lecture_0611.pdf)  
27. RoomPerfect™ Product Description, accessed May 26, 2026, [https://currants.info/wp-content/uploads/2019/10/roomperfect\_productdescription\_english.pdf](https://currants.info/wp-content/uploads/2019/10/roomperfect_productdescription_english.pdf)  
28. ARTA-HANDBOOK, accessed May 26, 2026, [https://artalabs.hr/AppNotes/ARTA%20Handbook%20Version%202.4%20English.pdf](https://artalabs.hr/AppNotes/ARTA%20Handbook%20Version%202.4%20English.pdf)  
29. LOUDSPEAKER CHARACTERIZATION MEASUREMENT DEPARTMENT OF ELECTRICAL ENGINEERING UNIVERSITY OF SYDNEY \- CSIRO people, accessed May 26, 2026, [https://people.csiro.au/-/media/people-finder/b/j/john-bunton/loudspeakercharacterizationandmeasurement\_short.pdf](https://people.csiro.au/-/media/people-finder/b/j/john-bunton/loudspeakercharacterizationandmeasurement_short.pdf)  
30. Chris A, Could you explain minimum phase analysis, please. \- Technical/Restorations, accessed May 26, 2026, [https://community.klipsch.com/topic/173932-chris-a-could-you-explain-minimum-phase-analysis-please/](https://community.klipsch.com/topic/173932-chris-a-could-you-explain-minimum-phase-analysis-please/)  
31. Group delay and phase delay \- Wikipedia, accessed May 26, 2026, [https://en.wikipedia.org/wiki/Group\_delay\_and\_phase\_delay](https://en.wikipedia.org/wiki/Group_delay_and_phase_delay)  
32. Really basic REW question \- Technical/Restorations \- The Klipsch Audio Community, accessed May 26, 2026, [https://community.klipsch.com/topic/201751-really-basic-rew-question/](https://community.klipsch.com/topic/201751-really-basic-rew-question/)  
33. Best practices for creating & adjusting room correction EQ filters : r/audiophile \- Reddit, accessed May 26, 2026, [https://www.reddit.com/r/audiophile/comments/mnuo5u/best\_practices\_for\_creating\_adjusting\_room/](https://www.reddit.com/r/audiophile/comments/mnuo5u/best_practices_for_creating_adjusting_room/)  
34. A New Approach to Small Room Acoustics \- RealTraps, accessed May 26, 2026, [https://realtraps.com/art\_small\_rooms.htm](https://realtraps.com/art_small_rooms.htm)  
35. Digital Room Correction \- DRC, accessed May 26, 2026, [https://drc-fir.sourceforge.net/doc/drc.html](https://drc-fir.sourceforge.net/doc/drc.html)  
36. Roon Room Correction \- HouseCurve, accessed May 26, 2026, [https://housecurve.com/docs/appnotes/roon](https://housecurve.com/docs/appnotes/roon)  
37. Trinnov Audio NOVA User Manual, accessed May 26, 2026, [https://www.trinnov.com/site/assets/files/5338/trinnov\_audio\_nova\_user\_manual.pdf](https://www.trinnov.com/site/assets/files/5338/trinnov_audio_nova_user_manual.pdf)  
38. Your room matters: it's time to talk room correction. \- Dynaudio, accessed May 26, 2026, [https://dynaudio.com/magazine/2017/november/ask-the-expert-whats-room-correction-and-the-new-roomadapt-feature](https://dynaudio.com/magazine/2017/november/ask-the-expert-whats-room-correction-and-the-new-roomadapt-feature)  
39. (PDF) Applications of a plane wave based room correction system for low frequencies using multiple loudspeakers \- ResearchGate, accessed May 26, 2026, [https://www.researchgate.net/publication/5327587\_Applications\_of\_a\_plane\_wave\_based\_room\_correction\_system\_for\_low\_frequencies\_using\_multiple\_loudspeakers](https://www.researchgate.net/publication/5327587_Applications_of_a_plane_wave_based_room_correction_system_for_low_frequencies_using_multiple_loudspeakers)  
40. From The Audiophile's Guide: Active Room Correction and Digital Signal \- PS Audio, accessed May 26, 2026, [https://www.psaudio.com/blogs/copper/from-em-the-audiophile-s-guide-em-active-room-correction-and-digital-signal-processing](https://www.psaudio.com/blogs/copper/from-em-the-audiophile-s-guide-em-active-room-correction-and-digital-signal-processing)  
41. How to think about room correction settings like frequency range and gain? | Page 3 | WiiM, accessed May 26, 2026, [https://forum.wiimhome.com/threads/how-to-think-about-room-correction-settings-like-frequency-range-and-gain.6560/page-3](https://forum.wiimhome.com/threads/how-to-think-about-room-correction-settings-like-frequency-range-and-gain.6560/page-3)  
42. altitude32 user guide \- Trinnov, accessed May 26, 2026, [https://www.trinnov.com/site/assets/files/1219/al32\_usman\_14\_10\_19\_he\_0001\_sd.pdf](https://www.trinnov.com/site/assets/files/1219/al32_usman_14_10_19_he_0001_sd.pdf)  
43. Trinnov Optimizer Features: According To L\&R & Excursion Curve, accessed May 26, 2026, [https://www.trinnov.com/en/blog/posts/trinnov-optimizer-features-according-to-l-r-excursion-curve/](https://www.trinnov.com/en/blog/posts/trinnov-optimizer-features-according-to-l-r-excursion-curve/)  
44. Symmetric Loudspeaker-Room Equalization Utilizing a Pairwise Channel Similarity Criterion | Request PDF \- ResearchGate, accessed May 26, 2026, [https://www.researchgate.net/publication/260502568\_Symmetric\_Loudspeaker-Room\_Equalization\_Utilizing\_a\_Pairwise\_Channel\_Similarity\_Criterion](https://www.researchgate.net/publication/260502568_Symmetric_Loudspeaker-Room_Equalization_Utilizing_a_Pairwise_Channel_Similarity_Criterion)  
45. ENTERTAINMENT WITHOUT BOUNDARIES \- AWS, accessed May 26, 2026, [https://mseaudio-site-files.s3.us-west-2.amazonaws.com/Phase+dARTS+whitepaper+2020-2.pdf](https://mseaudio-site-files.s3.us-west-2.amazonaws.com/Phase+dARTS+whitepaper+2020-2.pdf)  
46. Audyssey Labs' MultEQ \- Audioholics, accessed May 26, 2026, [https://www.audioholics.com/room-acoustics/audyssey-labs-multeq](https://www.audioholics.com/room-acoustics/audyssey-labs-multeq)  
47. Immersive Audio Signal Processing | PDF \- Scribd, accessed May 26, 2026, [https://www.scribd.com/document/400588062/immersive-audio-signal-processing](https://www.scribd.com/document/400588062/immersive-audio-signal-processing)  
48. A room acoustics measurement system using non-invasive ..., accessed May 26, 2026, [https://etheses.bham.ac.uk/891/1/Roper10Phd.pdf](https://etheses.bham.ac.uk/891/1/Roper10Phd.pdf)  
49. (PDF) Multichannel Room Correction with Focus Control \- Academia.edu, accessed May 26, 2026, [https://www.academia.edu/94398618/Multichannel\_Room\_Correction\_with\_Focus\_Control](https://www.academia.edu/94398618/Multichannel_Room_Correction_with_Focus_Control)  
50. Dirac Live Room Correction, accessed May 26, 2026, [https://www.dirac.com/products/room-correction](https://www.dirac.com/products/room-correction)  
51. FULLY AUTOMATIC LOUDSPEAKER-ROOM ADAPTATION – the ..., accessed May 26, 2026, [https://currants.info/wp-content/uploads/2026/03/RoomPerfect-AES\_paper.pdf](https://currants.info/wp-content/uploads/2026/03/RoomPerfect-AES_paper.pdf)  
52. reference manual \- Trinnov, accessed May 26, 2026, [https://www.trinnov.com/site/assets/files/1696/opt\_refman\_05\_12\_18\_pro\_sd.pdf](https://www.trinnov.com/site/assets/files/1696/opt_refman_05_12_18_pro_sd.pdf)  
53. 7370A SAM™ Studio Subwoofer \- Genelec.com, accessed May 26, 2026, [https://www.genelec.com/7370a](https://www.genelec.com/7370a)  
54. GLM 5 System Operating Manual \- Genelec Loudspeaker Manager GLM, accessed May 26, 2026, [https://downloads.ctfassets.net/4zjnzn055a4v/5vjR23qy2h89dIdSHAYN02/289952200d3c08ef4492e846895e7cd8/GLM\_5.0\_System\_Operating\_Manual\_\_2\_2024\_.pdf](https://downloads.ctfassets.net/4zjnzn055a4v/5vjR23qy2h89dIdSHAYN02/289952200d3c08ef4492e846895e7cd8/GLM_5.0_System_Operating_Manual__2_2024_.pdf)  
55. Equalization \- HouseCurve, accessed May 26, 2026, [https://housecurve.com/docs/tuning/equalization](https://housecurve.com/docs/tuning/equalization)  
56. Room correction with Acourate \- DSP \- Roon Labs Community, accessed May 26, 2026, [https://community.roonlabs.com/t/room-correction-with-acourate/22544](https://community.roonlabs.com/t/room-correction-with-acourate/22544)

[image1]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABMAAAAaCAYAAABVX2cEAAAA3klEQVR4XmNgGAWUgnlA/BmI/0PxAhRZCPjLgJAHYWdUaUyArBgb2AfEKuiC2AAjEG8H4vUMEMOCUKXBAJclGCAfiE2gbFyu+4MugAu8RWJ/YIAYxockpgbEnUh8vADZJaBwAfFvIoktA2IeJD5OAAqvzWhi6F7F5m2sADm8kMVABnRD+b+Q5PCCd+gCUABznTYQt6DJ4QS4vLCbASJ3D4g50eSwAhYg3osuCAVMDJhhhxMwA/EbID6JLoEEvgHxD3RBdLAKiD8yQNIXKF2B8h42oA/E2eiCo2AUDGkAAM4NNN65dbHtAAAAAElFTkSuQmCC>

[image2]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAwAAAAbCAYAAABIpm7EAAAAeklEQVR4XmNgGAXDAkQA8V8g/o8FJyKpA4PXUInFQLwAygaJlQNxEUIZBCxggChABs1YxMCAlQEioYMm3ggVxwBeDNglVjBgF2fYwYBdAiT2AV0QBJYxYGoQgYrxoYmDgTADqgYmKD8aSQwD2DAgwvsmEAuiSo+CIQ8AhVIhcfkOsEsAAAAASUVORK5CYII=>

[image3]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADoAAAAXCAYAAABaiVzAAAAA8klEQVR4Xu2VvQ4BQRSFL4IKEWriGbQKFYkH0GglWrXX4BlEIaL0ABKdGoVSISIaiQpnMis215Lxk8xm3S/5ijlnmjubnSESBEEQgk+ZB0FjAvcwwwsbpGAV1pifEoULuIZx1lkhDy8vbN23GpGGOziDIdZZI0F6mLYr28Kza21KAZ7giBd+QA05ZlnTyU0pkj6YHi/8gvr/vAYakHf+jArp/R1e+AV1E3oNpLIDDw24fdkuL2zTp8dBs06WZPk75OARDnlhC/WmuQcNO+uGK/sGddFt4JQXNijR/RlZkn4afk0EzuEKxlgXWOo8EARB+AuuF8MwbgOgib0AAAAASUVORK5CYII=>

[image4]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGoAAAAXCAYAAADjndqIAAACaklEQVR4Xu2Xy6tNYRjGX7dEuWTgKCWZmVAUJSUZCBOF/8FYBi4xMDIwQUoYMpLoEDIwRMmAkBKHklxLKbdyeZ6+89V3Hu9ae6+919p7xferp9N63rX2d77L+653mWUymUwm036WQx+h39AdaO7EcCnroScWnj0nsUyN7ISOJddnLSz6isQrYhf0K7nmb/HZurgBfYKmaGCITIMeQ8800DRcWF1cz/PgPUsd77B4VZgKPYJeQjMkNkxYZd5Bt6FJEhsIr+zvTelmo7aaf8938/1OzIHeQndtSAtRwBLoG3RBA8Nmn4WF3qwB4ab5GzJmvl/EYugrNCp+v6yDNlmYR9TGCXeUs8ZCWT+ugTYQs+SoBhz47vA2hGXL85VVFhbipAb6hBkZK4KnTmy3cN9eDbSFI9B56Ce0QWIeRRO/b76vfIYuq9knzy2UqVg6F1r4X1hWu4XzP6FmG4mTu6IB4bX5G/LQfN9jloXfuQdNllhVYiYovWTHIugLdFEDbaMoW1KK3lFsWz2/DLbfD6AX1nunxzGfqmnB361ml8yG3kC3rAUNDlP9tHhxo9aKn7Lf/A3pteuLXLPw/puvgQ5wTDYPKSvHfX779AM/GVgpeAinS2wgbDM/e6KXfmjugEaSa8J75jleHe+eU9APC+1xN3DcmeK9t9Dy18l16INVe+/VAieYnpJl497VxGPaexvKsjCWXC+wek5wykELJagT3JQDyfUhC9ndFGfUaBpmBNtkLjBPCv96Xc8l82s9n+Gp5cby2W4zoAnY9cUDtUdimcy/zZYKGiQ6dpn+C1ZX0CDRscuUyWQymWb4A3g1oqeldjF3AAAAAElFTkSuQmCC>

[image5]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADoAAAAXCAYAAABaiVzAAAAA9UlEQVR4Xu2VPwtBYRTGX1IyIJHVYPARLHYy26zK6nP4GJKMRgOT8gkYlLIYZBCZGPCc3HI93XT96V50fvUb3ue5vXW63XuMURRFUX6TEgf/SgruYYeLbyUOi7BMuiUKV3AMA9R9BRl4fmD99qgrQnACFzBCnW/IW5BhGrZsDU+28zsM4Q6mufAaGbJHWc3KP0kbHmCWCy+Q789poK5xzt+haa535rnwgr5xHkiyLYcv0oJHmOPCS2Qd8KCyKiSLUf4sA7gx1/t8J2nuBw1a56otewb5207hHIap852Cua2RGUzc166QFbKEIy7+jQoHiqIoCnEBE+4wWA9WHDQAAAAASUVORK5CYII=>

[image6]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABMAAAAWCAYAAAAinad/AAAA1ElEQVR4XmNgGAWUgl1A/J9ITDTAp2EVEP9GF8QFmBkgBp1Cl4ACXiA+iC6IC5QwQAzzQBPngNL8QNyELIEPfGTA9GIVEKtA2axAzI0khxegh5cWGp9oAAsvbJhksJ0BolEAScwOiLOR+EQDbK7QQeMTDUAG/UEXxAMCgFgYXRAEbBkghnWiS+AAX6D0LAZIDINBLAMkgcK8+BWIjwBxL0wBFhAIxK8YIGEL0k8x+MkAsfwEugQpgAWIfyHx0SOMJGAExHlQdjQQOyLJkQ0M0QUGLwAAm642kJBF2egAAAAASUVORK5CYII=>

[image7]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEcAAAAXCAYAAABZPlLoAAABPElEQVR4Xu2XOy9FQRSFFxq5CSU6rf7qRfwBhUSl1eIf0N5KQaLwCpX4BRJaBYmeRqMSuSEkJBKsnT1xx8p9NnIzM1+ycubstU+xdua8gEKh8M9MUjvUFjUqXrZsUt/UUjgfp56pj9+ODBmED+VGjYB5X1rMBQv/qsWIFXjPnBqp8wAP3o4JeM+pGikzAw99oUYTrK+uxZT5hIe2Z0475uF9J2qkjAXudEsZZ/C+RTWEdeq4hY6oQ+qA2qN2qTW7qB8Zgwd+V6MJ3Q4xGYbgga/VEKbgfftqpI6F7vT90suuWaZqPWjVL+tPXtAIPhzW59R2qNm628EkiYW375fHqGa/DLfw4Q1E9Sx5gg/pCn6b2Xoh8ivROnvuqNno3HZQIXAP3z1v4Vj9a+fNBhpvqUvxCmSaGtFioVBoxQ9+2FBynTkUmAAAAABJRU5ErkJggg==>

[image8]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAD4AAAAXCAYAAABTYvy6AAABOUlEQVR4Xu2Wuy6FQRSFFxqRUKLT6ulFvIBCotJq8Qa0KoWTKM5FqMQTSGgVJHoajUpECAmJBGtnT5yxcpxLc5IZ8yUr/8xe+y/WzPwXoFAokClqj9qlxsTLkh3qi1oJ8wnqiXr/6ciMQXjgKzUC5n1qMQcs2IsWI9bgPQtqpMwdPFQ7JuE9x2qkyhw80JkaLbC+Ry2mygc8kD3j7ViE9x2pkSoWptMxN07gfctqCJvU4R86oPapBlWjqtSG3dRvxuFh3tRoQbcLlARD8DCXagjT8L66GiljgTp9n3vZ7VVquwet+2395xnNUMNhfEpVQs3G3YZODgtm3+f7qGa/qdfwhRmI6tnxAF+AC/jRt/FS5I9E46y5oeajue38v+AWvuuv4Trz286XLTTf5ufiZc8sNarFQiEtvgFf/1BymgXtcwAAAABJRU5ErkJggg==>

[image9]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABcAAAAWCAYAAAArdgcFAAAA+klEQVR4XmNgGAUDCbiBeBcQ/wfi00DMiCpNFDiDLgAC0gwQQzmhfGEonwmuAjc4yQBRC8MY4CsQr0QTA7niB5oYPlDEgMNwkGAYmlgVVJxYgNVwO6igDZp4PFRcCE0cF8BqeAFU0AhNPBQqbo4mjgtgNbwJKqiHJh4IFY9GE8cFsBqeBhU0QBMPgYo7o4njAlgNh4W5JZp4LFQclEyJAVgNZ4cK0iS1gABIcBKa2DaoODIARbI4mhgM4DQcmytB/CAkPqg4AImhq4OBLgaInCi6BAgsB+K/UBqkCJRE0cEGIC5BE/sFxC+A+AkQP4bSr4F4MbKiUTAMAAD5dEPxh2vyigAAAABJRU5ErkJggg==>

[image10]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACUAAAAWCAYAAABHcFUAAAAAj0lEQVR4XmNgGAWjYBQMLDBDFxgMgA2IHwDxESBmRJUaeMAExBeB+D4Qc6LJDQqwHYg/AbEEusRgAPOB+A8Q66JLDAbQCsT/gdgOXWIgQTYDxFGR6BIDAVoYII5xQpcYCDAXiH8DsSa6xECAHUD8HohF0SXoDUAF5hkgvgvEHGhyAwZA1cygK8lHwSigFgAA2WAR1rwoB6gAAAAASUVORK5CYII=>