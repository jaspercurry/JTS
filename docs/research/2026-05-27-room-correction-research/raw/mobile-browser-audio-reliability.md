# JTS Mobile Browser Microphone Reliability — Research Report

## Executive summary

**Bottom line up front: the web platform is good enough for an opt-in, hardware-aware room-correction wizard but never good enough to trust blindly — JTS must verify the captured signal, not what the browser claims.** Across Safari (iOS/iPadOS), Chrome (desktop/Android) and Firefox, getUserMedia + AudioWorklet can capture 48 kHz PCM from class-compliant USB measurement microphones (UMIK‑1, UMIK‑2, iMM‑6C, UMM‑6) well enough to drive correction filters. But every layer of the stack — WebKit / CoreAudio's VPIO, Chromium's audio processing module, Android's USB HAL, Apple's AVAudioSession mode selection — is free to silently apply resampling, AGC, voice processing, or even substitute a different physical capsule, and the constraint API gives only weak guarantees.

Three concrete facts shape every design decision below:

1. `echoCancellation`, `autoGainControl`, `noiseSuppression`, `sampleRate` and `channelCount` are best-effort *hints*. WebKit bug 179411 ("getUserMedia echoCancellation constraint has no affect") was only RESOLVED FIXED in r252681 (2019-11-19); bug 286680, filed by youenn fablet himself on 2025-01-29, is currently NEW and shows that `{echoCancellation:{exact:false}}` still throws OverConstrained on WebKit Nightly. Chrome ties AGC bypass to `echoCancellation:false` and only exposes a real `voiceIsolation` constraint on ChromeOS as of Chrome 123 (per Per Åhgren's blink-dev Intent to Ship, 2024-01-26: *"The constraint only takes effect on platforms where there is low-level support for voice-isolation style denoising. Currently this is limited to a selected number of ChromeOS devices, but further platforms will be added."*).
2. iPhone 6s and later are hardware-locked to a 48 kHz sample rate (Apple Developer Forums thread 22197: *"The internal speaker on the iPhone 6S models only support a sample rate of 48kHz while previous iPhone models supported a collection of sample rates"*; corroborated in libpd GitHub issue #148). Asking for anything else forces a hidden resample.
3. Apple's documented escape from VPIO/AGC in native code is `AVAudioSession.Mode.measurement`. Safari does not expose this knob to JavaScript, so a web wizard cannot fully replicate the native bypass — only approximate it via `echoCancellation:false` and verification.

**The right pattern is a multi-layer smoke test** that (a) string-matches the device label, (b) probes a 1 kHz tone for peak/RMS/THD/SNR/DC offset/dropouts, (c) sanity-checks the frequency response against the loaded calibration file, (d) computes a 0–100 confidence score, and (e) refuses to ship a correction filter below a hard threshold. Every measurement bundle persists the full `MediaTrackSettings`, user agent, calibration hash, and per-check pass/fail.

**The browser is JTS's UI and capture transport; the Pi is the source of truth.** Treat any single measurement as suspect until the Pi has cross-validated it, and always offer a native-app and desktop-REW fallback for users whose phone fails the smoke test.

---

## Platform matrix

Legend: **OK** = works well; ⚠️ = works with caveats; ❌ = broken / unreliable. Confidence: **H** = spec / bug-tracker / vendor docs verified; **M** = multiple practitioner reports; **L** = anecdotal / single source.

| Capability | iOS Safari (Lightning + adapter) | iOS Safari (USB-C, iPhone 15+) | iPadOS Safari (USB-C) | Android Chrome | Android Firefox | Desktop Chrome | Desktop Safari | Desktop Firefox |
|---|---|---|---|---|---|---|---|---|
| getUserMedia({audio}) basic | OK (H) | OK (H) | OK (H) | OK (H) | OK (H) | OK (H) | OK (H) | OK (H) |
| External USB mic enumerated as distinct `audioinput` | ⚠️ Label populated only after permission grant (M) | ⚠️ Sometimes appears as generic "iPhone Microphone" until labels unlock (M) | OK (H) | ⚠️ Variable per OEM/Android version; react-native-webrtc issue #1116 documents `enumerateDevices()` returning only `audio-1` on some devices (M) | ⚠️ Same as Chrome (L) | OK (H) | OK (H) | OK (H) |
| `deviceId` stable across sessions | ⚠️ Rotates with cookies/storage clearing per W3C Media Capture spec §15.1.1: *"User Agents MUST rotate per-origin device identifiers when other persistent storage are cleared"* (H) | ⚠️ Same (H) | ⚠️ Same (H) | ⚠️ Same (H) | ⚠️ Same (H) | ⚠️ Same (H) | ⚠️ Same (H) | ⚠️ Same (H) |
| `sampleRate: 48000` honored | ⚠️ AudioContext may force 44.1/48 kHz at OS level; iPhone 6s+ hardware locked to 48 kHz; WebKit bug 217147 (fixed r267838, 2020) showed unsupported rates silently ended the track (M) | ⚠️ Same (M) | ⚠️ Same (M) | OK; resample is silent (M) | OK (M) | OK (H) | OK (H) | OK (H) |
| `echoCancellation:false` actually disables EC | OK since WebKit 179411 (RESOLVED FIXED r252681, 2019-11-19) (H) | OK (H) | OK (H) | ⚠️ Honored, but Kerry Davis on bug 179411 noted *"mobile chrome on Android did NOT default to filters disabled as is the case in desktop Chrome"*; verify via getSettings() (M) | OK (H) | OK; also disables AGC as a side-effect (H) | OK (H) | OK (H) |
| `autoGainControl:false` actually disables AGC | ⚠️ Per youennf comment #19 on bug 179411: *"When setting echoCancellation to false, we both disable AGC and echo cancellation"*; explicit AGC constraint tracked in WebKit bug 204444 (M) | ⚠️ Same (M) | ⚠️ Same (M) | ⚠️ Per addpipe.com: *"Chrome applies autoGainControl by default, but you can't control it through the autoGainControl constraint, to turn it off you actually need to set echoCancellation to false"*; flag `chrome://flags/#enable-webrtc-allow-input-volume-adjustment` can re-enable it (M) | OK (H) | ⚠️ Same (H) | ⚠️ Same (M) | OK; AGC off by default on Firefox (H) |
| `noiseSuppression:false` actually disables NS | OK (M) | OK (M) | OK (M) | ⚠️ APM may still process on some Android builds; verify (L) | OK (H) | OK (H) | OK (H) | OK (H) |
| `channelCount` honored | ⚠️ Often coerced to mono (M) | ⚠️ Same (M) | ⚠️ Same (M) | ⚠️ Same (M) | ⚠️ Same (M) | OK (H) | OK (H) | OK (H) |
| AudioWorklet stable | ⚠️ Shipped iOS 14.5 (Safari 14.1, WebKit bug 182506); iOS 16.2 bugs and iOS 17.6.1/18 dev-forum reports of glitched output (Apple Developer Forums 734378, 768347) (M) | ⚠️ Same (M) | OK (H) | OK (H) | OK (H) | OK (H) | OK (H) | OK (H) |
| UMIK-1 (USB Audio Class 1, 48 kHz fixed) | ⚠️ Needs Lightning-to-USB Camera Adapter; mic powered through adapter only (H) | OK direct plug, original UMIK-1 via USB Mini-B to USB-C cable, UMIK-1 v2 ships USB-C (per miniDSP setup guide) (H) | OK direct plug (H) | ⚠️ Hit-or-miss; AudioTool group notes the app often cannot programmatically choose between built-in and USB inputs, the OS routes USB as the "Main Mic" (M) | ⚠️ Same (L) | OK (H) | OK (H) | OK (H) |
| UMIK-2 (USB-C, UAC2, selectable 44.1–192 kHz, 32-bit ADC) | ⚠️ Same adapter requirement (H) | OK (H) | OK (H) | ⚠️ UAC2 not always supported pre-Android 10; varies by OEM (L) | ⚠️ Same (L) | ⚠️ Windows requires miniDSP ASIO driver; macOS/Linux/Android driverless per miniDSP product page (H) | OK (H) | OK (H) |
| iMM-6C (USB-C) / iMM-6 (TRRS) | iMM-6 via Lightning-to-3.5 mm adapter; iMM-6C requires USB-C adapter on Lightning iPhones (H) | OK (H) | OK (H) | OK on USB-C Android devices (M) | OK (L) | OK (H) | OK (H) | OK (H) |
| Built-in mic acceptable as fallback | ⚠️ Always processed (VPIO / voice isolation); response varies by model (H) | ⚠️ Same; HouseCurve publishes per-model compensation but stopped after iPhone 15 (H) | ⚠️ Large reflective surface skews HF (per a HouseCurve user comparing iPad Air 4 to iPhone 13 Pro) (M) | ❌ Highly variable across OEMs; not recommended (M) | ❌ Same (M) | ⚠️ Laptop mic processing varies (M) | ⚠️ macOS Voice Isolation may apply (M) | ⚠️ (M) |

---

## Recommended JTS smoke-test protocol

Run these in order **before** dispatching the long exponential sweep. Every check has pass / warn / fail thresholds; every measurement is persisted to the observability bundle.

### Pre-capture (UI-side, before getUserMedia)

1. **Secure context** — fail fast if `window.isSecureContext === false`. `navigator.mediaDevices` is undefined in insecure contexts.
2. **AudioWorklet support** — require `AudioContext.audioWorklet` to be present. Refuse to fall back to deprecated ScriptProcessor for measurement (it introduces extra latency and main-thread scheduling jitter).
3. **Browser allowlist (soft)** — UA sniff for known broken combos: iOS Safari < 14.5 (no AudioWorklet), iOS 16.2 (AudioWorklet stutter), iOS in-app WKWebView (bug 208667). Warn but don't block; let the smoke test do the real work.

### Post-permission (after getUserMedia resolves)

4. **Device label string match** against allowlist patterns:

   * `Umik|UMIK[-_ ]?1|UMIK[-_ ]?2` → external calibrated USB; expect 48 kHz native for UMIK-1 (per miniDSP product page: *"USB Audio class 1.0 Driverless interface"*), selectable 44.1–192 kHz for UMIK-2.
   * `iMM[-_ ]?6C?|Dayton.*iMM` → external calibrated USB-C/TRRS; per Dayton: *"individually hand-calibrated using a laboratory-standard measurement microphone"*.
   * `UMM[-_ ]?6|EMM[-_ ]?6` → external calibrated USB / XLR-via-interface.
   * `iPhone Microphone` / `iPad Microphone` / `Built-in` / empty / "Default" → built-in (degraded).
   * Anything else → "unknown external, no calibration file" warning.

   **Pass:** matches a known calibrated pattern AND a calibration file is loaded.
   **Warn:** unknown external mic.
   **Fail (block by default):** built-in, unless user explicitly opts into degraded mode.

5. **`getSettings()` flag check** — log every field; require:

   * `echoCancellation === false` → PASS; `true` → FAIL.
   * `autoGainControl === false` → PASS; `true` or `undefined` → WARN.
   * `noiseSuppression === false` → PASS; `true` → FAIL.
   * `sampleRate === 48000` → PASS; `44100` → PASS with note; anything else → WARN.
   * `channelCount` → informational; if a stereo mic returns mono, log it.

6. **Probe tone** — play a 5-second 1 kHz sinusoid at a fixed digital level (e.g., −20 dBFS) and capture:

   * Peak < −1 dBFS → PASS for headroom; ≥ −0.5 dBFS → FAIL (clipping).
   * RMS within ±6 dB of expected after the user-driven "comfortable level" step → PASS; outside ±12 dB → FAIL.
   * THD at the fundamental < 1 % → PASS; > 3 % → FAIL (clipping or limiter).
   * No spectral energy outside ±50 Hz of 1 kHz above −60 dB → PASS; otherwise WARN (noise or AGC pumping).

7. **Noise-floor / SNR check** — capture 3 s of silence:

   * A-weighted noise floor < −60 dBFS → PASS.
   * −60 to −50 dBFS → WARN (recommend retry).
   * > −50 dBFS → FAIL (HVAC, traffic, or AGC raising the floor).
   * SNR target: ≥ 40 dB between probe-tone RMS and silence RMS; < 25 dB → FAIL.

8. **DC offset** — buffer mean |μ| < 0.001 × full scale. Larger indicates a broken ADC or DC-coupling issue (some cheap USB DACs).

9. **Dropout / glitch detection** — during the probe tone, look for sample-to-sample discontinuities > 0.3 × full scale across consecutive frames; count `process()` callbacks where `inputs[0][0].length !== 128` (the Web Audio render quantum). Any dropped frames → WARN; > 1 % → FAIL.

10. **Frequency-response sanity** — play a 1-s pink-noise burst and compute the 1/3-octave smoothed spectrum after calibration. Expect ±3 dB from 100 Hz to 10 kHz close-field. Larger deviations indicate (a) wrong calibration file, (b) phone case obstructing the built-in mic, or (c) wrong device picked. Per the WiiM forum: *"the HouseCurve default for iPhone 15 was the bottom mic. Just switched to the front one and the treble peak disappeared"* — built-in mic selection matters.

11. **The real sweep**, with continuous monitoring:

    * Live peak detector; abort and re-prompt if clipping at any point.
    * RMS gate: if level ever drops below −90 dBFS for > 200 ms, mark "dropout" and offer retry.
    * USB mics lack a hardware timing reference to the Pi's DAC clock; use REW-style acoustic timing reference (a click before the sweep that lets the Pi cross-correlate), or rely on full impulse-response cross-correlation in post.

### Confidence score (composed)

* −30 if EC or NS reported true
* −20 if AGC reported true or undefined
* −20 if `sampleRate ≠ 48000`
* −15 if built-in mic
* −20 per smoke-test FAIL, −5 per WARN
* −10 if calibration file missing
* **Block** filter deployment at score < 50; **warn** at 50–75; **auto-deploy** above 75.

---

## Failure modes and detection strategies

| Failure mode | Likely cause | Detection | Mitigation |
|---|---|---|---|
| iOS Safari ignores `echoCancellation:false` | Pre-Safari 11 behavior (WebKit 179411 RESOLVED FIXED r252681, 2019-11-19). On 2025 WebKit Nightly, `{exact:false}` syntax fails OverConstrained (Bug 286680 NEW, filed by youenn fablet 2025-01-29). | `getSettings().echoCancellation === true` | Use plain `echoCancellation:false`, not `{exact:false}`. Re-apply via `track.applyConstraints({echoCancellation:false})` per youennf comment #7 on bug 179411. |
| iOS VPIO highpass strips bass | Safari uses CoreAudio VoiceProcessingIO when EC is on. Faber Acoustical: *"In order to avoid processing of the microphone signal by the operating system, to provide the highest quality signal for measurement purposes, SignalScope uses a 'measurement mode' offered by iOS/visionOS."* The native bypass is `AVAudioSession.Mode.measurement`; Safari does not expose this knob to JS. | Probe tone + sweep both show < −15 dB at 60 Hz vs expected. | Force `echoCancellation:false`. If still bad, recommend native fallback app. |
| Android Chrome forces APM | Per Kerry Davis on WebKit bug 179411: *"mobile chrome on Android did NOT default to filters disabled as is the case in desktop Chrome."* | `getSettings()` may misreport; corroborate via THD on the 1 kHz probe — APM signatures are comb-filtering and abrupt gain steps. | Set `echoCancellation:false` always; verify via the probe-tone smoke test. |
| External mic plugged but page selects built-in | iOS Safari may not auto-switch input on hot-plug; Android Chrome may not enumerate USB mic separately (react-native-webrtc issue #1116: *"`mediaDevices.enumerateDevices()` only have one audioinput device id: audio-1"*). | `getSettings().deviceId` doesn't match expected, or device label is "iPhone/iPad/Built-in microphone". | Pass `deviceId` explicitly in getUserMedia constraints. If label is still wrong, instruct user to disconnect Bluetooth/AirPods, replug mic, reload page. |
| Sample rate mismatch / silent resampling | iOS AudioContext historically locked to 44.1 kHz; iPhone 6s+ hardware locked to 48 kHz (Apple Developer Forums 22197); UMIK-1 native 48 kHz; UMIK-2 selectable 44.1–192 kHz. Setting unusual rates (e.g., 24 kHz) caused stream end in WebKit 217147 (RESOLVED FIXED r267838, 2020). | `getSettings().sampleRate` vs `audioContext.sampleRate` mismatch reveals internal resampling. | Always run AudioContext at 48 kHz. If browser refuses, resample on the Pi using a calibration-aware filter (sox / libsamplerate VHQ). |
| AudioWorklet broken on iOS Safari | iOS 16.2 dev-forum reports (Apple Developer 734378) of stuttering and the worklet not starting at all; iOS 17.6.1 reports in thread 768347. | Compare expected sample count vs actual; check for repeated input frames. | If detected during the probe-tone step, fall back to MediaRecorder + WAV decode (less precise) or direct the user to Chrome / desktop. |
| Phone case obstructs built-in mic | User has a thick case covering the bottom mic port. | Frequency-response sanity check shows > 6 dB rolloff above 4 kHz. | Tell user to remove case or rotate phone; HouseCurve notes iPhone has three mics and the default may not be optimal. |
| Built-in mic compensation drift | HouseCurve maintains per-model compensation curves but, per AVForums discussion, *"Buchardt … have decided to stop allowing use of the iPhone app after the iPhone 15"* because mic response shifts model-to-model. | No automatic check possible. | Hard-allowlist of known compensated devices; reject newer/unknown models for built-in mode. |
| Bluetooth / AirPods steal input | iOS pairs input and output for Bluetooth devices; AirPods become the mic at 16 kHz on HFP. | Device label contains "AirPods" / "Bluetooth", or `sampleRate` drops to 16 kHz. | Detect by label substring; refuse, prompt to disable Bluetooth in Control Center. |
| CarPlay context | HouseCurve docs: *"When CarPlay is used to connect to an automobile audio system, be aware that the selected microphone will be the automobile (head unit) microphone, not the iPhone/iPad microphone."* | Device label / context. | Detect and refuse. |
| Headphone-out connected to system | User wired headphone-out to the speaker for the sweep, then iOS rerouted output to built-in speakers under getUserMedia (well-known iOS behavior documented across WebKit issues and the Medium IOS Safari article). | Acoustic timing reference fails or speaker isn't audible. | Use AirPlay or a direct cable + manual loopback test; measure speaker latency separately. |
| WKWebView in third-party iOS app | getUserMedia blocked in WKWebView historically (WebKit bug 208667). | Detect via UA + permission failure. | Require Safari proper, not in-app browser. |
| Chrome flag `enable-webrtc-allow-input-volume-adjustment` enabled | User has Chrome auto-adjusting input volume. | Cannot detect from JS. | Document in troubleshooting; recommend disabling. |

---

## Bundle observability fields (concrete schema)

Persist this JSON alongside every WAV capture so the Pi can post-hoc reject bad measurements and users can submit reproducible bug reports.

```jsonc
{
  "schema_version": "1.0",
  "session_id": "uuid-v4",
  "timestamp_iso": "2026-05-26T12:34:56.789Z",
  "client": {
    "user_agent": "...",
    "ua_parsed": { "browser": "Safari", "version": "26.1", "os": "iOS",
                   "os_version": "26.1", "model_hint": "iPhone15,3" },
    "is_secure_context": true,
    "audio_worklet_supported": true,
    "device_pixel_ratio": 3
  },
  "constraints_requested": {
    "audio": {
      "deviceId":        { "exact": "..." },
      "sampleRate":      48000,
      "channelCount":    1,
      "echoCancellation": false,
      "autoGainControl":  false,
      "noiseSuppression": false,
      "voiceIsolation":   false   // honored on ChromeOS only as of Chrome 123
    }
  },
  "track_settings_actual": {          // from track.getSettings()
    "deviceId":         "...",
    "groupId":          "...",
    "sampleRate":       48000,
    "channelCount":     1,
    "echoCancellation": false,
    "autoGainControl":  false,
    "noiseSuppression": false,
    "latency":          0.01
  },
  "track_capabilities":     { /* track.getCapabilities() */ },
  "supported_constraints":  { /* navigator.mediaDevices.getSupportedConstraints() */ },
  "audio_context": {
    "sample_rate":   48000,
    "base_latency":  0.005,
    "output_latency": 0.02,
    "state":         "running"
  },
  "device": {
    "label_raw":        "miniDSP UMIK-1 Gain: 18dB",
    "label_normalized": "umik-1",
    "device_hash":      "sha256(deviceId || origin_salt)",  // privacy-safe stable ID
    "match_category":   "calibrated_external",  // calibrated_external | unknown_external | builtin | bluetooth
    "match_confidence": 0.95
  },
  "calibration": {
    "file_present":          true,
    "file_filename":         "7001870.txt",
    "file_sha256":           "...",
    "file_source":           "user_upload | minidsp_serial | dayton_serial | bundled",
    "file_sensitivity_dbfs": -38.2,
    "file_orientation":      "0deg | 90deg"
  },
  "smoke_tests": {
    "secure_context":             { "pass": true },
    "audio_worklet":              { "pass": true },
    "ua_allowlist":               { "pass": true,  "note": null },
    "device_label_match":         { "pass": true,  "category": "calibrated_external" },
    "settings_ec_false":          { "pass": true,  "value": false },
    "settings_ns_false":          { "pass": true,  "value": false },
    "settings_agc_false":         { "pass": true,  "value": false },
    "settings_sample_rate":       { "pass": true,  "value": 48000, "expected": 48000 },
    "probe_tone_peak_dbfs":       { "pass": true,  "value": -8.3,  "threshold_max": -1.0 },
    "probe_tone_rms_dbfs":        { "pass": true,  "value": -22.1 },
    "probe_tone_thd_pct":         { "pass": true,  "value": 0.18,  "threshold_max": 1.0 },
    "noise_floor_dbfs_a":         { "pass": true,  "value": -68.2, "threshold_max": -55.0 },
    "snr_db":                     { "pass": true,  "value": 46.1 },
    "dc_offset":                  { "pass": true,  "value": 0.0003 },
    "dropouts_count":             { "pass": true,  "value": 0 },
    "freq_response_flatness_db":  { "pass": true,  "value": 1.8,   "threshold_max": 3.0 }
  },
  "capture": {
    "sweep_duration_s":         8,
    "sweep_f_start_hz":         20,
    "sweep_f_end_hz":           20000,
    "sweep_type":               "exponential",
    "expected_samples":         384000,
    "actual_samples":           384000,
    "missing_frames":           0,
    "worklet_callbacks_count":  3000,
    "worklet_underruns":        0,
    "clock_drift_ppm_estimate": null,    // computed if acoustic reference is present
    "wav_sha256":               "..."
  },
  "confidence_score": 92,
  "decision": "deploy | warn | block",
  "user_overrides": {
    "accepted_builtin_fallback": false,
    "skipped_smoke_tests":       []
  }
}
```

---

## Fallback recommendations (priority-ordered)

1. **External calibrated mic + supported browser (preferred).** Order of preference for user hardware:
   * **UMIK-2** (USB-C, 32-bit ADC, selectable 44.1–192 kHz, 1/2″ capsule, low noise — per miniDSP: *"sample rates are selectable from 44.1 to 192 kHz for compatibility with the widest possible range of measurement and recording software"*) on USB-C iPhone, iPad, or Android — best.
   * **UMIK-1** (original USB Mini-B, 48 kHz fixed; UMIK-1 v2 ships USB-C — per miniDSP setup guide: *"connect the UMIK-1 v2 or UMIK-2 to your computer using the supplied USB Type-A to USB-C cable"*) — well-known reference; great if user already owns one.
   * **iMM-6C** (USB-C, $44.98 at Parts Express part 390-813, individually hand-calibrated) — great budget option; USB Audio Class compliant.
   * **UMM-6 / EMM-6** + interface — only if user already owns; not phone-friendly.

2. **Desktop bridge (REW workflow).** Document a path where the user plugs their UMIK into a laptop, runs REW with a JTS-supplied target curve and sweep, exports an `.frd` impulse response or `.wav`, then uploads it to the JTS web UI. This is the most reliable measurement path and should be promoted prominently to users who already know REW.

3. **Native companion app (iOS + Android).** When JTS resources allow, a small native app using `AVAudioSession.Mode.measurement` on iOS (per Apple docs and Faber Acoustical, this is the canonical VPIO/AGC bypass) and `AudioRecord` with `AudioSource.UNPROCESSED` on Android (per Android Developers: *"Most of the audio sources (including DEFAULT) apply processing to the audio signal. To record raw audio select UNPROCESSED. … If it is not, try using VOICE_RECOGNITION instead, which does not employ AGC or noise suppression"*) gives reliable raw capture and authoritative device enumeration. This is the only safe path for users on devices where browser smoke tests routinely fail.

4. **Built-in mic degraded mode.** Allowed only if the user explicitly opts in via a dialog that explains "Results will be approximate. We can correct major bass problems, but treble correction will be unreliable." Restrict to a hard-allowlisted device list (HouseCurve approach — and even HouseCurve's compensation library effectively stops at iPhone 15, which is the current ceiling for trustworthy built-in correction).

5. **Manual REW / measurement-tool import.** Accept `.frd`, `.txt` (REW measurement export), and `.wav` (impulse response) uploads. The escape hatch for power users and for unsupported phones.

**Wizard UX flow:** detect mic → if calibrated external is connected, run the automated smoke test → if not, present the user with a choice: { use external mic (with shopping links to Dayton iMM-6C and miniDSP UMIK-1/2) | use desktop bridge | use built-in degraded | upload REW measurement }.

---

## Known bugs / caveats as of 2026

### WebKit (iOS / iPadOS Safari)
* **Bug 179411** — *"getUserMedia echoCancellation constraint has no affect"* — **RESOLVED FIXED** in r252681 (2019-11-19). youennf, comment #19: *"When setting echoCancellation to false, we both disable AGC and echo cancellation. Please file a follow-up bug if you would like to have a specific autoGainControl handling."* Comment #24: *"I filed bug 204444 to keep track of adding support for the autoGainControl constraint."* https://bugs.webkit.org/show_bug.cgi?id=179411
* **Bug 286680** — *"getUserMedia({audio:{echoCancellation:{exact:false}}}) is failing with OverConstrained error"* — **NEW**, filed by youenn fablet 2025-01-29. Implication: prefer plain `{echoCancellation:false}`, not `{exact:false}`, on WebKit. https://lists.webkit.org/pipermail/webkit-unassigned/2025-January/1206186.html
* **Bug 217147** — *"getUserMedia with sampleRate constraints may fail"* — **RESOLVED FIXED** r267838, 2020-10-01. Cause: unsupported output sample rates caused the MediaStreamTrack to end immediately. Lesson: only request 44 100 or 48 000. https://bugs.webkit.org/show_bug.cgi?id=217147
* **Bug 192932** — referenced in the JTS task brief as VPIO context; could not be retrieved during research (Bugzilla redirected to an unrelated bug across multiple attempts). The practical VPIO behavior is fully documented through bug 179411 and the AVAudioSession documentation; recommend updating internal references to point there.
* **Bug 179220** — *"enumerateDevices(), device ids change on page refresh"* — informational; per W3C Media Capture spec, `deviceId` is a cookie-class persistent storage subject to clearing rules. https://bugs.webkit.org/show_bug.cgi?id=179220
* **Bug 208667** — *"getUserMedia does not work in WKWebView-based browsers like Chrome, Firefox"* — long-standing. On iOS, only Safari proper reliably supports getUserMedia. iOS Chrome and Firefox use WKWebView and have historically been broken. https://bugs.webkit.org/show_bug.cgi?id=208667
* **Bug 252465** — *"In PWA, HTML Video Element may be unable to play stream from getUserMedia()"* — relevant if JTS users add the wizard to Home Screen as a PWA; test explicitly. https://bugs.webkit.org/show_bug.cgi?id=252465
* **Bug 182506** — AudioWorklet implementation; shipped iOS 14.5. https://bugs.webkit.org/show_bug.cgi?id=182506
* **iOS AudioContext sample-rate quirk** — Safari historically locked AudioContext to 44.1 kHz even when hardware ran at 48 kHz; iPhone 6s+ are hardware-locked to 48 kHz (Apple Developer Forums thread 22197: *"The internal speaker on the iPhone 6S models only support a sample rate of 48kHz"*; libpd issue #148 confirms). Mismatches force resampling.
* **iOS audio routing on getUserMedia** — iOS reroutes output to built-in speakers when the mic is acquired, even with headphones connected; `setSinkId` is not fully supported.
* **iOS 16.2 / 17.6.1 / 18 AudioWorklet bugs** — Apple Developer Forums threads 734378 and 768347 show worklet output not starting or audio glitching when combined with getUserMedia.
* **iOS Safari iframe restriction** — getUserMedia is not allowed in cross-origin iframes on Safari (per addpipe.com test reports).
* **Safari 26.0** (build 20622.1.22) released 2025-09-15. Per the WebKit blog post 2025-09-15: *"Safari 26.0 also adds support for ALAC and PCM audio in MediaRecorder"* — useful if JTS ever needs to use MediaRecorder as an AudioWorklet fallback, but the public release notes do not document any `echoCancellation` / `autoGainControl` / `sampleRate` / USB-C enumeration changes that affect JTS.

### Chromium (Chrome desktop, Chrome Android)
* **`voiceIsolation` MediaTrackConstraint** — Intent to Ship by Per Åhgren on blink-dev 2024-01-26, targeting Chrome 123 desktop. Verbatim: *"The constraint only takes effect on platforms where there is low-level support for voice-isolation style denoising. Currently this is limited to a selected number of ChromeOS devices, but further platforms will be added."* Does NOT control Chrome's own APM. https://groups.google.com/a/chromium.org/g/blink-dev/c/hPMvCu-3iPA, https://chromestatus.com/feature/5106413661847552
* **Chrome APM tied to `echoCancellation`** — per addpipe.com testing: *"Chrome applies autoGainControl by default, but you can't control it through the autoGainControl constraint, to turn it off you actually need to set echoCancellation to false."* Always set `echoCancellation:false` first.
* **Android Chrome historically did not default APM off** — Kerry Davis on WebKit bug 179411: *"mobile chrome on Android did NOT default to filters disabled as is the case in desktop Chrome."* Verify per version via `getSettings()`.
* **Chrome flag `chrome://flags/#enable-webrtc-allow-input-volume-adjustment`** can re-enable AGC even when JS disables it.
* **Chrome flag `--use-file-for-fake-audio-capture`** is useful for CI testing the smoke-test pipeline.
* **react-native-webrtc issue #1116** — Android phones may expose only `audio-1` from `enumerateDevices()` and not list USB-C mics as selectable inputs.
* **Chrome 64 origin trial: "Disabling hardware noise suppression"** — Chrome blog notes that when `echoCancellation` is enabled, Chrome temporarily disables macOS hardware noise suppression. Out of scope for JTS (we want EC off), but worth knowing if comparing measurements.

### Mozilla Firefox
* Firefox honors `echoCancellation`, `autoGainControl`, `noiseSuppression` constraints more aggressively than Chrome (per addpipe.com testing). AGC defaults to false on Firefox; NS defaults to true. Always set explicit values.

### Practitioner-reported issues (HouseCurve, REW, miniDSP, Dayton)
* **HouseCurve docs (Greg Wilding):** *"The iPhone/iPad microphones have a surprisingly flat response and are quite acceptable for tuning an audio system. When compared to an external calibrated microphone, they do show some roll off at the extreme ends of the frequency spectrum (below 60 Hz and above 16 kHz). HouseCurve compensates for this by applying a moderate boost."* https://housecurve.com/docs/usage/microphones
* **HouseCurve docs (CarPlay caveat):** *"When CarPlay is used to connect to an automobile audio system, be aware that the selected microphone will be the automobile (head unit) microphone, not the iPhone/iPad microphone."*
* **WiiM forum** (user comparing iPhone built-in mic positions): *"Oh wait, didn't realise the iPhone has 3 mics and the default for HouseCurve was the bottom one. Just switched to the front one and the treble peak disappeared."* → for built-in mode, mic selection matters and is not always exposed.
* **WiiM forum** (on per-model calibration limits): *"Buchardt, for example, state that they find iPhone mics to be poor but somewhat consistent across each model number. … they've decided to stop allowing use of the iPhone app after the iPhone 15."*
* **miniDSP forum, UMIK-2 firmware bug** (miniDSP DevTeam): *"That low frequency changes based on sample rate is unfortunately a little bug that made its way on the latest firmware build. There is an HPF on the onboard ADC that needs to be reconfigured based on the sample rate. The higher the sample rate, the higher the cutoff frequency. We're doing a simple firmware build now to solve that issue. To get accurate data, please stick to 48 kHz for time being."* → use UMIK-2 at 48 kHz unless the user has confirmed firmware.
* **miniDSP forum / WiiM beta** (USB on Android): UMIK-1 / UMIK-2 plug directly into USB-C iPads and modern Android devices, but on Lightning iPads they need the Camera Adapter and on older Android they need OTG.
* **WiiM Room Correction Android beta** — widely reported as unreliable with built-in mics because of inconsistent OEM mic responses. Reinforces the case for external calibrated mics on Android.
* **AudioTool Google Group:** *"My understanding is that from Lollipop onwards, some devices support USB microphones natively (i.e. without special driver code). In those cases, plugging in a USB mic will automatically result in the mic being used — it will appear as the Main Mic — and programmers do not have any control over that behaviour."*
* **Faber Acoustical (Vision Pro article, 2024):** *"In order to avoid processing of the microphone signal by the operating system, to provide the highest quality signal for measurement purposes, SignalScope uses a 'measurement mode' offered by iOS/visionOS."* https://blog.faberacoustical.com/wpblog/2024/news/measured-apple-vision-pro-microphone-directivity-and-frequency-response/
* **AVAudioEngine voiceProcessing (WWDC 2019 session 510):** *"When enabled, extra signal processing is applied on the incoming audio."* Implication: anything that routes through VPIO will fight your sweep.
* **W3C MediaCapture spec on deviceId:** *"deviceId is to be treated as other persistent storage mechanisms such as cookies … User Agents MUST rotate per-origin device identifiers when other persistent storage are cleared."* Never assume cross-session stability; persist by name match.

### Capture transport: AudioWorklet → POST WAV/PCM (current) vs WebSocket streaming vs WebRTC
* **WebRTC is correctly rejected.** Opus / AAC are lossy; their psychoacoustic masking obliterates measurement precision in the upper bands. REW, HouseCurve, and miniDSP all use uncompressed PCM internally.
* **AudioWorklet + POSTed WAV (the current JTS design) is the right choice for room correction:**
  * Pros: lossless 32-bit float capture; debuggable (every bundle is a self-contained WAV inspectable in Audacity / REW); resilient to network blips (one POST at end vs continuous streaming); simple recovery (retry on POST fail); easy to bundle observability JSON alongside.
  * Cons: higher latency to first analysis (wait for sweep end + upload); larger payload (~7 MB for 8 s × 48 kHz × 32-bit stereo, ~3.5 MB mono); user must keep tab in foreground.
* **WebSocket / raw PCM streaming alternative:** lower memory pressure for long captures; allows a live VU meter during capture; but adds a second moving part (WS reconnect logic), and on mobile a background socket may be killed by power management. **Recommended only for the probe-tone / live VU phase**, with a final POST of the canonical WAV for the sweep itself.
* **AudioWorklet quirks on mobile Safari:** AudioWorklet shipped in Safari 14.1 / iOS 14.5 (per WebKit bug 182506 and caniuse), but iOS 16.2 saw documented breakage (Apple Developer Forums 734378) and intermittent issues persist in iOS 17/18 dev forum threads. **Always test smoke-test capture (the probe tone) before the sweep so AudioWorklet bugs fail the wizard cheaply, not after a 10-second sweep.** Web Audio render quantum is 128 samples (~2.67 ms at 48 kHz); buffer in the worklet and emit larger chunks via `port.postMessage` to keep the main thread happy.

---

## UX copy for low-confidence cases

Tone target: honest, non-blaming, actionable, brief. Avoid scare-quotes and ALL-CAPS. Always give the user a concrete next action.

**Mic processing detected**

> Your phone is applying voice processing (noise reduction or automatic gain) to the microphone, which would distort the measurement. We tried to turn this off, but the browser refused.
>
> **Recommended:** plug in a USB measurement microphone (UMIK-1, UMIK-2 or iMM-6C) and reload — these bypass phone processing. If that's not available, you can continue with the built-in mic but corrections will be approximate.
>
> [Continue with built-in (approximate)] [I have a USB mic] [Show me supported mics]

**Sample-rate mismatch**

> Your browser is recording at {actual} Hz, but we expected {expected} Hz. We'll resample on the speaker, which usually works fine, but it adds a tiny amount of uncertainty above 15 kHz. Bass and midrange correction are unaffected.
>
> [Continue] [Try again]

**Background noise too high**

> The room is louder than we can measure accurately right now. Background noise: {dB} dBFS (we'd like to see below −55 dBFS). Common culprits: HVAC, fans, traffic, conversation in adjacent rooms.
>
> **Try:** wait for things to quiet down, close windows, turn off fans, then re-run the measurement.
>
> [Retry] [Continue anyway (degraded)]

**Mic obstructed or clipping**

> The microphone signal looks {clipped | muffled}.
> *Clipped*: the level is too high — please lower your speaker volume by about 6 dB and try again.
> *Muffled*: it looks like the phone's mic is covered — please remove your phone case, or check that the mic port is unobstructed.
>
> [Retry]

**External mic not detected, falling back to built-in**

> We couldn't find your USB microphone. You're currently using the phone's built-in microphone, which works but isn't as accurate above 10 kHz.
>
> **To use the external mic:** disconnect any Bluetooth audio (AirPods, headphones), make sure the mic is plugged in, then [Reload]. If you don't have a USB measurement mic, you can [Continue with built-in (approximate)] or [Use a laptop instead].

**Confidence below threshold**

> We measured your room, but the audio path had {N} issues we couldn't work around: {list}. We're not confident enough in this measurement to deploy a correction filter automatically — a bad filter can sound worse than no filter.
>
> [Try again with a different mic] [Use desktop measurement instead] [Deploy anyway (advanced — I understand the risk)]

**iOS Safari + Bluetooth headset**

> Your AirPods are currently selected as the microphone. Bluetooth mics are limited to 16 kHz and apply heavy voice processing — they're useless for room correction. Please disconnect them in Control Center, then [Retry].

---

## Open questions / unresolved risks

1. **Safari's underlying AVAudioSession mode for getUserMedia is undocumented.** Whether Safari uses `.voiceChat`, `.videoChat`, `.default`, or `.measurement` when JS passes `echoCancellation:false` is not public. Without source inspection of `Source/WebCore/platform/mediastream/mac/CoreAudioCaptureSource.cpp`, we cannot promise users that JTS will get the same flat low-end response as a native app using `AVAudioSession.Mode.measurement`.
2. **WebKit bug 192932** could not be located via the public Bugzilla; either the ID is incorrect or the bug is restricted. Practical VPIO behavior is captured by bug 179411 and AVAudioSession docs; recommend updating internal references to those.
3. **Android Chrome USB-C mic enumeration is fundamentally inconsistent** across OEMs and Android versions. Anecdotal reports (react-native-webrtc #1116) show `enumerateDevices()` returning only one entry. Need empirical testing on Pixel 8/9, Samsung Galaxy S24/S25, and a budget OEM device before claiming Android browser support.
4. **iPhone 16/17 built-in mic compensation is unknown.** HouseCurve effectively stopped per-model calibration after iPhone 15. For 2026 phones, treat built-in mode as "experimental" and rely on smoke tests.
5. **UMIK-2 firmware HPF bug at non-48 kHz rates** — has miniDSP shipped the promised firmware update? Worth verifying and either documenting the workaround (force 48 kHz) or detecting via probe sweep.
6. **WebRTC `voiceIsolation` constraint adoption** — by mid-2026 may have expanded beyond ChromeOS. Recheck Chrome Platform Status periodically; if it lands on desktop/Android, JTS should explicitly request `voiceIsolation:false`.
7. **Clock drift** between the phone's audio clock and the Pi's playback clock is an inherent issue with USB mics that lack a timing reference channel. REW solves this with an acoustic reference click; JTS should adopt the same approach, or accept some smear in the impulse response and rely on cross-correlation. Needs empirical measurement.
8. **iOS / iPadOS Safari 26.x release notes** were not fully reviewed for getUserMedia regressions. Safari 26.0 (2025-09-15) adds ALAC and PCM MediaRecorder support, but echoCancellation/sampleRate behavior changes (if any) were not in the WWDC 2025 announcement. Watch each iOS 26.x dot release.
9. **PWA-installed JTS** — WebKit bug 252465 reports getUserMedia stream playback issues inside PWAs. If JTS users add the wizard to Home Screen, test this path explicitly.
10. **Permission persistence on iOS Safari** — Safari may forget mic permission per-session; the smoke test should tolerate multiple `getUserMedia` calls within the same session (avoid the historic WebKit bug 179363 family — primarily a video issue but worth confirming an audio analog doesn't exist).