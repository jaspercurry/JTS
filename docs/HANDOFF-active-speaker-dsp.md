# HANDOFF: active speaker DSP commissioning

> **Status: planning baseline.** Created 2026-05-25 from three local
> deep-research reports on DIY DSP speaker commissioning; updated
> 2026-05-26 with the proposal-v3 active speaker commissioning
> methodology. This is the canonical handoff for JTS speakers where
> CamillaDSP directly drives woofer, midrange, and/or tweeter
> amplifier channels. Current JTS production hardware still uses a
> stereo Apple USB-C dongle passthrough path; active crossover
> hardware is future work.

> **Implementation status, 2026-06-03:** A0 schema substrate has
> started. `jasper.active_speaker` now defines import-cheap,
> side-effect-free preset, channel-map, safety-envelope, crossover
> region, and speaker-baseline profile models with validation and
> tests. A1 template work has also started:
> `jasper.active_speaker.camilla_yaml.emit_active_speaker_startup_config`
> emits muted/protected CamillaDSP startup templates with explicit
> active-hardware playback device input, `volume_limit: 0.0`, startup
> headroom, tweeter protective HP, per-driver mute, and per-driver
> limiter chains. `/sound/` has an Advanced speaker setup entry point;
> its **Check environment** action is read-only: it calls
> `/sound/active-speaker/environment` and displays the read-only
> environment report without touching live audio.
> The same card now includes a lightweight Output setup UI over
> `/sound/output-topology`; it can render detected physical outputs,
> speaker groups, assigned/unassigned lanes, safety evidence, and
> no-audio setup templates for mono/stereo passive, mono/stereo active
> 2-way, and mono/stereo active 3-way wiring. Subwoofer is an optional
> add-on to the current draft rather than a duplicated template matrix:
> when an unused physical output exists, the UI adds one `subwoofer`
> group and records it in `routing.subwoofer_group_ids`. Saving that map
> only persists the output topology JSON and runs backend validation; it
> does not load CamillaDSP or emit sound. The UI organizes this work as
> collapsible task cards — choose layout, research drivers, map and verify
> outputs, then stage/load/start quiet. The open card is derived from existing
> output/topology/startup state plus transient browser intent; it does not
> create a separate persisted wizard-progress source of truth, and earlier
> cards remain editable.
> `/sound/active-speaker/channel-identity` now exposes and updates
> operator-confirmed physical channel identity evidence for the saved
> topology. The UI can mark or clear an assigned channel as physically
> verified after wiring inspection, dummy-load/DMM checks, or a future
> low-level channel test. This still does not play audio, reload
> CamillaDSP, or grant playback permission; tweeter protection and path
> safety remain separate blockers.
> The card can also arm and stop a no-audio safe-playback session
> through `jasper.active_speaker.safe_playback`; arming only persists
> safety state after the environment load gate passes, and Stop is
> idempotent. `jasper.active_speaker.tone_plan` can now prepare a
> bounded channel-test intent from the preset, armed session, and
> current environment report, but the returned plan still says
> `would_play: false` and cannot emit sound.
> `jasper.active_speaker.playback` now adds the first playback seam:
> `/sound/active-speaker/play-tone` validates the current plan and
> armed safe session, then renders a bounded multi-channel WAV artifact
> through the default backend. It sets `audio_emitted: false`, does not
> open ALSA, does not reload CamillaDSP, and does not touch live volume
> unless the operator explicitly enables the lab `aplay` backend with
> `JASPER_ACTIVE_SPEAKER_TONE_BACKEND=aplay`,
> `JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO=1`, and
> `JASPER_ACTIVE_SPEAKER_TEST_PCM=<pcm>`. Even then, the audible path is
> short, clamped, topology-readiness-gated, protected-startup-load-gated, and
> limited to woofer, mid, and subwoofer roles in this slice. The shared
> `woofer_mid_low_level_v1` policy is recorded in readiness reports, tone
> plans, playback results, and generated artifact metadata; tweeter/
> compression-driver and unlisted roles stay blocked even if the lab backend
> is enabled. The backend also enforces its own artifact envelope (48 kHz max,
> 16 channels max, 100-500 ms, -80..-45 dBFS) and keeps a small rolling set of
> generated artifacts so `/var/lib/jasper` cannot grow without bound during
> repeated checks. The safe-session state now owns a quiet-start lifecycle:
> every armed session starts in `floor_required`; an audible test above the
> calibration floor is rejected until the same session and target has a
> successful floor-level audible result; Stop, expiry, or target change resets
> the lifecycle back to `floor_required`.
> `jasper.active_speaker.readiness` now provides the read-only
> `/sound/active-speaker/playback-readiness` gate for one saved topology
> target. It combines safe-session state, output topology, channel
> identity, tweeter protection, clock-domain status, active-config/path
> safety, calibration-level bounds, protected startup-load state, Stop
> availability, and active tone backend status. By default, preconditions can
> pass while `playback_allowed` remains false; only explicit lab backend
> enablement plus a loaded/current protected startup config can turn it true
> for woofer, mid, and subwoofer topology targets. If CamillaDSP is no longer
> running the loaded startup config path, readiness blocks before the playback
> backend is reached. The `/sound/` safety card now presents those same
> gates as an operator sequence — check environment, stage protected
> startup, check protected path, load protected startup, arm safe session,
> check one target, reset to the level floor, then verify an artifact before
> any audible test, confirm floor audio, then raise slowly. The readiness card
> also summarizes the selected target, backend, rollback state, test level, and
> "why sound is blocked" reasons. For tweeter/compression-driver targets it
> also includes a no-audio **Horn bring-up readiness** packet that distinguishes
> blocked, manual floor-test candidate, and guided floor-test candidate states
> from topology/protection/startup-load/Stop/level/mic evidence; it still
> reports `audio_allowed: false`. Tweeter/horn target actions stay locked in
> this UI slice even when a lab backend is enabled. The safety card now also
> includes a read-only **Commissioning rehearsal** packet from
> `jasper.active_speaker.commissioning` and
> `/sound/active-speaker/commissioning-rehearsal`. It rehearses the durable
> sequence from saved output map through safe-session/floor readiness without
> playing sound, reloading CamillaDSP, or storing wizard progress; target
> readiness, artifact verification, and floor-audio confirmation remain
> explicit operator-selected actions.
> `/sound/` also includes a driver-research helper for active-crossover
> planning. It generates a prompt from the current output roles and accepts a
> pasted JSON object with kind `jts_active_crossover_driver_research`.
> That JSON is only shape-checked and summarized client-side in this slice;
> it is not persisted, trusted as measurement evidence, translated into
> CamillaDSP filters, or applied.
> `jasper.active_speaker.staging` now provides the first build-specific
> protected startup staging slice. The default preset is
> `jasper/active_speaker/presets/epique_e150he44_eminence_f110m8_safe_v1.json`
> for a mono Dayton Epique E150HE-44 woofer plus Eminence F110M-8
> compression-driver cabinet. `/sound/active-speaker/channel-protection`
> records either physical compression-driver protection evidence or a
> software-guarded bring-up request. The software-guard state is deliberately
> still a topology/playback blocker; it only lets
> `/sound/active-speaker/stage-config` write a no-load muted/protected
> CamillaDSP candidate plus
> `/var/lib/jasper/active_speaker_staged_config.json` evidence. That route
> refuses missing guard intent, non-contiguous output assignments, and missing
> explicit hardware playback device evidence. For software-guarded bring-up it
> also records evidence that the generated graph contains startup mute,
> protective high-pass, startup headroom, limiter, and no-load/no-playback
> guarantees. It does not load the config, reload CamillaDSP, emit sound, or
> grant playback permission.
> `jasper.active_speaker.bringup` and
> `/sound/active-speaker/bringup-preflight` now make the product fork explicit:
> **manual guarded bring-up** can continue without a microphone for an operator
> with a known plan, while **guided calibration** requires working microphone
> capture. Manual guarded bring-up still requires saved topology, verified
> physical output identity, a ready active-speaker environment/load gate,
> staged guard evidence, Stop availability, and the calibration level at the
> floor. A calibrated mic upgrades guided confidence; an unchecked or clipping
> mic blocks guided calibration without pretending manual setup is calibrated.
> `jasper.active_speaker.startup_load` and
> `/sound/active-speaker/startup-load` now add the first guarded CamillaDSP
> reload boundary for this workstream. The UI can show the current
> startup-load preflight/state, POST
> `/sound/active-speaker/load-startup-config` to load the staged
> muted/protected startup graph, and POST
> `/sound/active-speaker/rollback-startup-config` to restore the config that
> was active before the load. Loading is blocked unless the staged candidate
> exists, validates as a JTS active-speaker startup config, path-safety
> evidence is hardware-probe-backed, assigned physical outputs are verified,
> the staged metadata still matches the current saved topology, the software
> guard evidence is intact, the calibration level is at the floor, Stop is
> available, no tone playback is active, and the current CamillaDSP config
> path exists as a rollback anchor. This is a DSP reload slice only: it does
> not generate samples, open ALSA directly, raise volume, or authorize
> playback. The load transaction persists a rollback anchor during the shared
> `dsp_apply` persist phase, so a missing rollback breadcrumb causes immediate
> rollback instead of leaving CamillaDSP pointed at an active startup graph
> with no recovery state.
> `/sound/active-speaker/check-path-safety` now writes the first
> hardware-probe-backed path-safety evidence artifact at
> `/var/lib/jasper/active_speaker_path_safety.json` (or
> `JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE`). This is still a no-audio
> proof step: it inspects the saved topology, staged protected candidate,
> backend calibration-level guard, and current CamillaDSP config path. The
> artifact binds that proof to the topology identity, target assignments,
> staged config path and hash, and rollback config path and hash. The evidence
> scope is `load_only_no_audio`; a passing check may unlock the startup-load
> path but never tones or normal playback by itself. Startup load rejects stale
> evidence and asks the user to run Check protected path again if the saved
> topology, staged candidate, or rollback anchor changed after the proof. The
> check is deliberately honest about rollback: if the current config is raw
> stereo or unknown custom DSP, rollback is treated as unprotected and the
> evidence stays blocked until a protected rollback strategy exists.
> `jasper-active-speaker startup-template` can write one of these
> candidate templates from a preset JSON file and run
> `camilladsp --check` when the binary is available. The guarded web load
> path above is now the only product route that may reload this staged graph,
> and it still does not authorize sound. The first packaged
> worked-example preset is
> `jasper/active_speaker/presets/bc_de250_dayton_e150he44_v1.json`; the
> current no-audio default preset is the Epique/F110M safe bring-up profile
> above because it matches Jasper's immediate cabinet build.
> `jasper-active-speaker path-audit` now exposes the deterministic
> audible-path safety checklist and can evaluate operator evidence,
> but operator evidence is not enough to permit active config loading.
> `jasper-active-speaker environment-probe` is the first read-only
> environment evidence pass: it inspects ALSA playback devices, the
> current CamillaDSP statefile/config shape, `devices.volume_limit`,
> output channel count, optional `camilladsp --check`, and optional
> path-safety evidence. It now also reports a `safe_playback` block
> whose `playback_allowed` value is always `false` in the current
> implementation. It never plays audio, reloads CamillaDSP, or mutates
> state. `ok_to_load_active_config` can be true only when an active
> startup candidate, valid CamillaDSP preflight, and hardware-probe-backed
> path-safety evidence all pass; even that does **not** authorize tone
> playback until physical channel identity and a level-limited tone
> generator with emergency stop exist.
> Current next step: use the guarded startup-load preflight on hardware, verify
> rollback, then exercise the lab-gated low-level woofer/mid test path before
> any compression-driver output is enabled. The first audible horn slice must
> remain separately gated, high-passed, level-bounded, Stop-controlled,
> microphone-aware when available, and start at the test-level floor.

## Current Operational Truth

Active speaker DSP is a separate layer from room correction and from
preference voicing. Room correction asks, "what should be compensated
at this listening position?" Preference voicing asks, "what tonal
tilt does this listener like?" Active speaker commissioning asks,
"what should this speaker be before the room is considered?"

For JTS, that means:

- The current `/correction/` wizard must not rewrite crossover,
  polarity, per-driver gain, driver delay, or limiter policy.
- Active speaker commissioning is **Layer A: speaker baseline**:
  per-driver linearization, baffle-step compensation, acoustic-target
  crossover, polarity, time alignment, gain trim, and per-driver
  limiters. It is measured with room-immune or quasi-anechoic
  techniques and stored as a versioned speaker-baseline profile.
- Room correction is **Layer B**: modal-region EQ and listening-area
  compensation. It is measured at the listening position(s), lives in
  the stereo domain, and must not silently alter Layer A.
- Preference voicing is **Layer C**: house curve, bass/tilt choices,
  and subjective "brighter / warmer / more bass" tuning. It is
  reversible taste shaping, stored separately from both Layer A and
  Layer B.
- In active mode, "flat" must mean **protected speaker baseline with
  room/preference EQ bypassed**, not an identity full-range
  `/etc/camilladsp/v1.yml` path. Resetting to identity can send
  full-range content to a tweeter.
- A future active speaker profile is a versioned speaker baseline,
  not a room-correction session. Room correction and preference EQ
  stack with that baseline, but in a CamillaDSP graph they normally
  live on the stereo input pair before the per-driver split.
- Every measurement bundle should eventually record the active
  speaker profile ID so later analysis knows what acoustic baseline
  was measured.

The existing deployed audio topology is not yet active 2-way ready:

- Music flows through CamillaDSP to a stereo Apple USB-C dongle.
- TTS/cues currently bypass CamillaDSP and enter `jasper-outputd`,
  where they sum with post-DSP content before the Apple USB-C dongle.
- Active crossover output needs a stable multi-output map. For a
  mono active cabinet this is at least two physical outputs
  (`woofer`, `tweeter`). For a stereo active pair this is four
  physical outputs (`left_woofer`, `left_tweeter`,
  `right_woofer`, `right_tweeter`).
- The current active-speaker preset `channel_map` is a **logical
  CamillaDSP output map**, not a complete physical-DAC setup UI. A
  future DAC/speaker setup surface must discover or accept the
  physical device lanes, let users group lanes into speakers, swap
  left/right, mark passive speakers, assign active driver roles up to
  3-way, and identify subwoofer outputs. Do not bake HiFiBerry DAC-X8,
  Apple dongle, or any other DAC-specific physical assumption into
  `tone_plan` or the dry playback artifact backend.
- That future setup surface now has a backend substrate:
  `jasper.output_topology` and `/sound/output-topology` persist a
  versioned, complete-replacement topology draft at
  `/var/lib/jasper/output_topology.json`. It can describe physical DAC
  lanes, speaker groups, passive/active modes, up-to-3-way driver
  roles, subwoofers, approximate placement, identity verification, and
  tweeter protection status. Its hardware shape comes from the static
  DAC profile registry, so known profiles such as Apple USB-C, DAC8x,
  and dual-Apple 4ch get consistent labels/output counts and clock-domain
  reporting. It deliberately has no audio side effects; active-speaker tone
  playback and active CamillaDSP loading must still pass through their own
  safety gates.
- The current `/sound/` UI composes one optional subwoofer add-on with
  any mono/stereo draft when a spare physical output exists. Keep that
  compositional model; do not add separate `stereo_active_2way_plus_sub`
  template families unless the topology contract itself changes.
- The topology substrate now has a separate channel-identity report and
  update route (`/sound/active-speaker/channel-identity`). Treat that
  evidence as an operator-confirmed fact about physical wiring, not as
  permission to emit sound. Marking a tweeter channel verified does not
  satisfy tweeter protection, path safety, startup/reload safety, or
  future level/mic gates.
- The topology substrate also records tweeter/compression-driver
  protection evidence via `/sound/active-speaker/channel-protection`.
  Marking protection present is a human/operator fact about the physical
  build; it is required before staging the Epique/F110M protected startup
  candidate, but it still does not load DSP or authorize playback.
- Before tweeter hardware is connected, all audible paths must be
  proven to pass through the same protected crossover path. A TTS
  bypass into a raw active amp channel is a driver-damage hazard.
  This applies to renderers, TTS/cues, `/correction/` sweeps,
  autolevel/test tones, USB Audio Input, startup/reload states, and
  any direct `jasper_out` rollback path.

## Layer Boundary

Keep DSP ownership separate, and be explicit about logical ownership
versus physical CamillaDSP placement. The v3 plan uses this model:

- **Layer A: speaker baseline.** Driver linearization, BSC, acoustic
  crossover, polarity, delay, gain, and per-driver limiters. Measured
  with near-field, null-depth, gated summed response, plus designer
  bench measurements. BSC may be physically pre-split; crossover,
  EQ, delay, and limiters are per-driver after split.
- **Layer B: room correction.** Modal-region EQ and listening-area
  correction. Measured at listening position(s). Lives on the stereo
  input pair before split.
- **Layer C: preference voicing.** Target tilt, house curve, and
  subjective bass/treble taste. Derived from published targets and
  user feedback. Lives on the stereo input pair as a reversible
  profile.

The practical CamillaDSP shape for active hardware is:

```text
source/renderers
  -> stereo-domain guards: rumble HP, headroom
  -> Layer B: room correction when enabled
  -> Layer C: target/preference voicing when enabled
  -> Layer A pre-split pieces: baffle-step / global baseline EQ
  -> N-way routing / channel map
  -> Layer A per-driver pieces: crossover, driver EQ, delay, polarity, gain
  -> per-driver limiter / protection guard
  -> physical outputs
```

The speaker baseline is the thing that makes the box a coherent
speaker. It should be commissioned once per hardware build and changed
deliberately. Room correction is re-run for a room/listening area.
Preference EQ is user taste and should always be reversible.
Baffle-step compensation is a speaker-baseline decision even when it
is physically placed before the 2-to-4 or 2-to-6 split on the stereo
pair. The profile schema must represent both logical ownership and
physical filter placement.

Do not confuse active-speaker `channel_map` ownership with the
final-output DAC8x route knob in
[HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md).
`JASPER_OUTPUT_DAC_ROUTE=mono:N` / `stereo:L,R` is a pre-active,
1-indexed, final-output alias for lab/single-amp commissioning wiring.
It keeps ordinary stereo output audible on explicit DAC8x physical
channels. A loaded active-speaker baseline instead owns a zero-indexed
CamillaDSP channel map, per-driver filters, limiters, startup mutes,
and the safety gates that must protect direct-connected drivers.
The persisted output topology sits between those two layers: it names
which physical DAC lane belongs to which speaker/driver role, but it is
not itself a CamillaDSP config and cannot authorize playback.
It also records a clock-domain report for the detected final-output
device. Today that report is intentionally a single-device boundary:
JTS can describe a coherent DAC8x or Apple output device, but it does
not product-support aggregating multiple USB DACs for active crossover.
Multiple USB DACs remain future lab work until JTS can measure and
compensate inter-device skew/drift before any sound-emitting path uses
them.

## Hard Safety Rules

These are not UX polish; they are anti-smoke rules.

- Do not connect the tweeter until channel identity, gain staging,
  and protective high-pass routing have been proven at low level.
- Treat a physical series protection capacitor on the tweeter as the
  preferred bench-safety path when available, but do not design the product as
  if most users will have one. If the operator does not have hardware
  protection, the supported product path is **software-guarded bring-up**, not a
  fake "physical protection present" checkbox: JTS may stage a
  no-load/no-playback candidate only after proving startup mute, protective
  high-pass, startup headroom, limiter, and volume ceiling evidence. Later
  audible slices must continue from that evidence, reset the test level to the
  floor, and keep Stop available before allowing any compression-driver tone.
- A CamillaDSP high-pass and limiter do not protect against wrong
  wiring, wrong channel maps, startup pops, DC faults, `jasper-camilla`
  not running, or a bypass path.
- Start with conservative output gain, tweeter muted, room correction
  disabled, and a temporary protective tweeter high-pass above the
  planned crossover.
- Never sweep a raw tweeter below its safe range just because an LLM
  or simulator says it is probably fine.
- A CamillaDSP limiter is a final clip stop, not a full thermal or
  excursion model. Driver protection needs gain structure, crossover
  limits, physical protection, and measured validation.
- New active configs should load with all physical outputs muted or
  routed to dummy loads until binding-post/channel identity is
  verified electrically.

## Default Commissioning Stance

The 2026-05-26 v3 proposal makes this a preset-first system. The
product does not ask end users to design crossovers from scratch.
Instead, a speaker designer creates a driver-set preset once, using
the engineering workflow below; the consumer wizard refines that
preset for the specific unit and room.

The default stance:

- Support both 2-way and 3-way active speakers through the same
  generic preset schema and N-way CamillaDSP template.
- Use an acoustic Linkwitz-Riley target by default. LR4 is the
  normal starting point; LR2 is rare and polarity-sensitive; LR8 is
  reserved for drivers that need stronger out-of-band isolation.
- Treat that as an acoustic target, not merely "insert electrical LR4
  biquads." The drivers, cabinet, protection capacitor, baffle
  diffraction, horn/waveguide, and acoustic center all shape the
  final acoustic slopes.
- Use IIR biquads for the first production baseline: low latency,
  simple CPU budget, inspectable filters, and no pre-ringing.
- Reserve FIR (`Conv`) for explicit later modes: global excess-phase
  correction, linear-phase experiments, or non-minimum-phase
  driver-inverse work after latency, CPU, headroom, and pre-ringing
  are all audited.
- Choose crossover frequency from the actual drivers and enclosure:
  tweeter safe operating range and distortion, woofer breakup and
  directivity, center-to-center spacing, baffle geometry, target SPL,
  and off-axis behavior. Do not hard-code a universal frequency.
- Store every accepted baseline as a versioned `speaker_baseline`,
  distinct from room-correction sessions and preference profiles.

For Jasper's own active bring-up build, the current no-audio default preset
is a Dayton Epique E150HE-44 plus Eminence F110M-8 2-way:
2.5 kHz LR4, non-inverted, woofer delay search range 0.0-0.6 ms,
physical compression-driver protection preferred, software-guarded no-load
staging allowed, and startup-muted outputs.
That preset is intentionally conservative for first power-up; final
crossover frequency, polarity, delay, gain trim, limiter thresholds, and EQ
must come from measurement with the actual horn/waveguide, baffle, enclosure,
amplifier gain, and microphone. The data-only default lives at
`jasper/active_speaker/presets/epique_e150he44_eminence_f110m8_safe_v1.json`.

The proposal-v3 worked example remains a B&C DE250 plus Dayton Epique
E150HE-44 2-way: 1.6 kHz LR4,
non-inverted, likely woofer delay around 0.05-0.30 ms, large tweeter
trim, conservative tweeter limiter, and a temporary protective
tweeter HP around 2x Fc during commissioning. Treat those as worked
example values, not project-wide defaults. They become defaults only
inside a named preset for that exact driver/horn/baffle/amp/channel
map combination. The first data-only version lives at
`jasper/active_speaker/presets/bc_de250_dayton_e150he44_v1.json`; it
is a worked example, not commissioned evidence.

## Measurement Protocol

Proposal v3 splits measurements into two paths: the engineering path
that creates presets, and the consumer wizard that verifies/refines a
known preset on a real speaker.

### Consumer Wizard Triad

The in-room wizard uses three complementary measurements. None is
sufficient alone; together they provide a practical room-immune Layer
A check.

1. **Near-field per-driver capture** measures individual driver
   magnitude and diagnostic phase while overwhelming room reflections.
   The mic is placed very close to the radiating surface: cone/dust
   cap for woofer or mid, dome/ribbon surface for tweeter, horn mouth
   for a compression-driver horn. This is not a free-field response
   and does not prove the acoustic sum, but it catches driver and
   assembly deviations against the preset envelope.
2. **Null-depth optimization** proves polarity and relative delay at
   each crossover. With the planned crossover active, invert one
   adjacent driver through the mixer and sweep the crossover band.
   Walk delay in small steps and maximize the inverted-polarity null.
   For a healthy LR4 preset, a centered null above roughly 25 dB is a
   strong pass signal; under roughly 20 dB should trigger delay,
   polarity, wiring, or hardware investigation.
3. **Gated at-position summed measurement** validates the direct
   summed response through the crossover region. The mic moves to the
   actual listening position, the wizard runs an ESS sweep with the
   full crossover engaged, gates before the first reflection, and only
   trusts the response above the gate-derived low-frequency limit.

Frequency budget:

- Above roughly 500-700 Hz in normal rooms: near-field, null-depth,
  and gated summed data can validate crossover behavior.
- Around 300-500 Hz: confidence is lower. A 3-way lower crossover in
  this region must lean harder on the engineering preset and should
  be labeled reduced-confidence unless the room geometry supports a
  longer gate.
- Below roughly 300 Hz: do not pretend in-room single-position data is
  a clean speaker baseline. Hand fine work to Layer B room correction.

### Engineering Path For Presets

Every curated preset is generated once by the speaker designer using
the higher-rigor path:

- impedance / bench data where available, so protection and excursion
  assumptions are not guessed from SPL alone;
- per-driver in-box measurements with no crossover: gated far-field
  on the design axis plus near-field captures for low-frequency
  extension;
- NF/FF merge with baffle diffraction modeling, e.g. VituixCAD
  Merger, to create an anechoic-equivalent reference response;
- crossover simulation against acoustic targets, including vertical
  polar prediction and deep-null simulation;
- CamillaDSP YAML generation and `camilladsp --check` validation;
- re-measurement with the actual CamillaDSP profile loaded;
- distortion / level escalation for conservative limiter settings;
- preset freeze with expected envelopes, safe sweep ranges, delay
  ranges, polarity, limiter values, BSC parameters, and safety
  thresholds.

### Browser And Phone Capture Requirements

The phone is a smart microphone, not the analysis engine. The DSP host
generates sweeps, receives raw PCM, deconvolves/gates/analyzes, and
stores the session. The browser streams lossless binary PCM over
WebSocket. Do not use WebRTC/Opus for measurement transport.

The first wizard step must verify:

- selected input device and selected calibration file;
- echo cancellation, AGC, and noise suppression requested off and
  behaviorally sanity-checked;
- received sample rate / channel count / level are plausible;
- known-level test tone produces clean capture with enough SNR;
- the loaded calibration curve is displayed before proceeding.

Missing or wrong microphone calibration is a blocking error, not a
warning.

## Delay, Phase, and Null Verification

Delay alignment is measured, not guessed.

- Do not assume "delay the tweeter." Delay whichever acoustic source
  arrives earlier after measurement. A horn can make the tweeter
  acoustically later, which may mean the woofer receives delay.
- Compare woofer and tweeter phase traces through at least roughly
  one octave around the crossover.
- Use summed response and reverse-polarity null depth as practical
  validation. After alignment, invert one driver; a strong, centered
  null around the crossover is the clearest quick proof that the
  branches are meeting as intended on the design axis.
- Validate off-axis after the on-axis null looks good. A crossover can
  sum acceptably on-axis while creating vertical holes in the
  listening window.
- Group delay, impulse response, and step response are supporting
  views; they are not substitutes for phase-aware summation.
- Suggested acceptance gate: no "commissioned" label until the
  measured in-phase sum and the reverse-polarity null are both
  captured after loading the actual CamillaDSP profile.

## CamillaDSP Profile Architecture

The future active speaker path should use bounded profile templates,
not freeform YAML generated by an LLM.

Baseline profile shape for 2-way and 3-way speakers:

```text
stereo source
  -> optional Layer B / C stereo-domain filters
  -> Layer A pre-split baseline filters such as BSC
  -> explicit split_2way or split_3way mixer
  -> per-driver crossover filters
  -> per-driver EQ needed to hit the acoustic target
  -> per-driver delay / polarity / gain trim
  -> per-driver limiter / protection block
  -> output device
```

For a 2-way stereo speaker pair, the split maps stereo input to four
outputs: woofer L/R and tweeter L/R. For a 3-way pair, it maps to six
outputs: woofer L/R, mid L/R, and tweeter L/R. A mono cabinet can use
the same schema with a one-input variant, but the first JTS schema
should not special-case mono at the expense of clarity.

Per-driver chain order is fixed unless a named preset explicitly
overrides it:

```text
crossover(s) -> in-band driver EQ -> delay -> gain trim -> limiter
```

Important implementation implications:

- Channel labels must be explicit and persisted.
- A commissioning-safe profile should start with tweeter outputs
  muted or heavily protected.
- Polarity inversion belongs in the mixer mapping (`inverted: true`),
  not as an implicit negative gain hidden in a filter list.
- The midrange chain in a 3-way preset normally has both a high-pass
  at the lower crossover and a low-pass at the upper crossover.
- Generated configs should be validated before load, and rollback
  should be obvious.
- Candidate primitive set to preserve in schemas/tests:
  `BiquadCombo` with `LinkwitzRileyLowpass` /
  `LinkwitzRileyHighpass`, `Biquad` PEQ/shelf filters, `Delay` in
  milliseconds or samples, `Gain`, mixer `inverted: true` for
  polarity, `Limiter` with `clip_limit` / `soft_clip`, `Compressor`
  as the separate attack/release dynamics tool, and `Conv` for
  future FIR.
- Limiters belong last in each per-driver chain so they see the
  signal actually headed to the DAC/amp. Reserve negative headroom
  before positive EQ, BSC, or driver-linearization boosts.
- Bypassing a mixer that changes channel count can break a CamillaDSP
  pipeline; the profile should make bypass points intentional.
- The active baseline profile should live separately from room
  correction profiles under `/var/lib/jasper`, with its own bundle
  metadata and accepted/rejected state.

Current no-apply template command:

```sh
jasper-active-speaker startup-template ./preset.json \
  --playback-device hw:MultiChannelDAC \
  --output ./active_speaker_startup.yml
```

This command writes a candidate YAML file and runs `camilladsp
--check` if the binary is installed. A missing validator is reported
as `Validation: missing` and does not load or apply anything.

Current no-hardware path safety command:

```sh
jasper-active-speaker path-audit --requirements
jasper-active-speaker path-audit ./path_safety_evidence.json
jasper-active-speaker path-probe \
  --current-config ./active_speaker_startup.yml \
  --output ./path_safety_evidence.json
jasper-active-speaker environment-probe --json
jasper-active-speaker environment-probe \
  --config ./active_speaker_startup.yml \
  --path-safety-evidence ./path_safety_evidence.json
```

The evidence form must pass before a future loader is allowed to
touch active hardware, but a passing operator checklist is not itself
permission to load an active config. `path-audit` reports both
`requirements_met` and `ok_to_load_active_config`; the latter is true
only when evidence is marked as hardware-probe-backed. Evidence must
declare `"evidence_source": "operator"` or `"hardware_probe"` so future
loaders never infer trust level from a missing field. `path-probe` and the
`/sound/active-speaker/check-path-safety` route now populate the first
hardware-probe-backed form for the startup-load preflight. It is a local
state inspection, not an audio probe: it does not reload CamillaDSP, open ALSA,
or play tones. It can still block on missing staged evidence, unverified
physical outputs, an unreadable calibration-level guard, or an unprotected
rollback target. The startup loader also rechecks the evidence binding against
the current staged candidate and rollback anchor; stale evidence is reported as
`evidence_stale` and must be refreshed before loading. `environment-probe` adds
real read-only ALSA and CamillaDSP
config/statefile inspection plus a `safe_playback` readiness block.
`safe_playback` is not a permission grant: it reports environment readiness
but never authorizes audio by itself.
`jasper.active_speaker.readiness` is the deterministic pre-playback gate for
one selected saved topology target. `/sound/active-speaker/playback-readiness`
combines safe-session state, saved output topology, target assignment, channel
identity, tweeter protection, clock-domain status, active-config/path safety,
calibration-level bounds, protected startup-load state, Stop availability, and
tone-backend status. It still emits no sound; it returns `preconditions_passed`
separately from
`playback_allowed` so artifact verification can proceed while audible playback
stays disabled. `playback_allowed` can become true only for woofer, mid, or
subwoofer topology targets when the explicit lab `aplay` backend is enabled and
the protected startup DSP state says the loaded config is still the current
CamillaDSP config with a rollback anchor. The playback backend requires the
same role policy and loaded-startup proof before allowing `aplay`; the
readiness route remains the layer that reads live startup/CamillaDSP state. The
probe still does not perform physical channel verification or generate
hardware-probe-backed path-safety evidence by itself. For a tweeter/
compression-driver target, the report includes a `compression_driver` section
with `audio_allowed: false` that distinguishes blocked evidence, manual
floor-test candidate evidence, and guided floor-test candidate evidence from
the saved output map, protection mode, loaded protected DSP, Stop/session
state, calibration floor, and operator-observed mic status.

`jasper.active_speaker.safe_playback` is the first no-audio session substrate
for that future work. It writes
`/var/lib/jasper/active_speaker_safe_playback.json` by default, reports
`playback_allowed: false` in every state, expires armed sessions, and makes
Stop idempotent. `/sound/active-speaker/arm` calls the environment probe and
only creates an armed session when `ok_to_load_active_config` is true;
`/sound/active-speaker/stop` stops any existing session. Neither endpoint
plays tones, reloads CamillaDSP, or changes volume. The persisted environment
summary stores config classification and filename only, not full local paths.
The same state file carries `quiet_start` evidence for the current safety
session. Artifact-only results never confirm the floor. A completed audible
floor-level result records `floor_confirmed` for the stable target signature
(`speaker_group_id`, role, output index); raised audible tests are then allowed
only for that same target and armed session. Changing target, stopping, or
letting the session expire clears that evidence.

`jasper.active_speaker.tone_plan` is the first deterministic channel-test
intent contract. `/sound/active-speaker/tone-targets` lists preset-derived
output targets; `/sound/active-speaker/tone-plan` takes a target and returns a
bounded sine-tone plan only when the safe session is armed and the current
environment load gate is ready. It clamps level and duration, derives
role-appropriate band limits from the preset crossover regions, and always
returns `would_play: false`, `playback_allowed: false`, and
`tone_playback_implemented: false` in this build. It is a contract for future
playback code, not a sound-emitting backend.

`jasper.active_speaker.playback` is the first backend seam for executing that
intent. The default backend is still no-audio: it writes a bounded
multi-channel WAV plus JSON metadata under
`/var/lib/jasper/active_speaker_tone_artifacts` (overridable in tests) with
only the selected logical output channel populated. The backend enforces the
same resource envelope at the writer boundary, not just in the web route:
sample rate is capped at 48 kHz, artifacts are capped at 16 channels, duration
clamps to 100-500 ms, and level clamps to -80..-45 dBFS. It keeps the newest
24 artifact sets by default (override:
`JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_RETENTION`, capped at 100) and prunes
older generated `tone_*.wav` / `tone_*.json` pairs after each successful
render. `/sound/active-speaker/play-tone` returns the plan, playback result,
and updated safe-session summary. The optional lab `aplay` backend can emit
audio only when `JASPER_ACTIVE_SPEAKER_TONE_BACKEND=aplay`,
`JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO=1`, and
`JASPER_ACTIVE_SPEAKER_TEST_PCM=<pcm>` are all set, the saved topology target
passes readiness, and the target is not a tweeter/compression driver. This is
useful for verifying target selection, channel count, level clamp, artifact
schema, logging, retention, and Stop semantics before richer measurement
automation exists.

`jasper.active_speaker.calibration_level` owns the commissioning test-signal
level contract. It deliberately separates calibration level from normal system
volume: the operator controls the requested test level, JTS clamps it to a
small safe envelope, and the default is the minimum (`-80 dBFS`). As of
2026-06-03 the level is a backend-owned persisted guard at
`/var/lib/jasper/active_speaker_calibration_level.json` (test override:
`JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE`). The `/sound/` card updates
that state through `/sound/active-speaker/calibration-level`; upward movement
is limited to one 1 dB backend transition, while lowering, reset, Stop, and
mic-clipping resets can return directly to the floor. The same route also
accepts `action=observe` with an operator-observed capture dBFS reading; that
records the coarse mic-meter status (`unmeasured`, `too_quiet`, `low`,
`usable`, `too_loud`, `clipping`) without changing the requested test level
unless clipping forces a floor reset. Tone-plan, readiness, and artifact
routes read the accepted persisted level rather than trusting request-local
`level_dbfs`. No current code raises listening volume, writes live CamillaDSP
volume, emits samples, or treats the slider or mic observation as permission to
play. This is still not real microphone capture or calibrated SPL; it is the
operator-observed feedback loop the first audible slice can consume.

`jasper.active_speaker.bringup` owns the read-only preflight packet for the
horn-bring-up product decision. It composes output topology, channel identity,
staged software-guard evidence, calibration-level floor state, safe-session
state, tone-backend status, and coarse microphone readiness into two bounded
modes:

- **Manual guarded bring-up**: available without a microphone for users who
  already know the crossover plan, but only after topology, output identity,
  active-speaker environment/load gate, staged guard evidence, Stop, and
  level-floor gates pass.
- **Guided calibration**: requires the same gates plus working microphone
  capture. A calibrated mic enables absolute guidance; an uncalibrated but
  working mic can provide relative safety feedback only. JTS must not label
  unmeasured/manual work as calibrated.

## Deterministic Tooling Roadmap

Code should eventually own:

1. Active topology detection: output channel count, named physical
   channel map, and "all audible paths are crossover-protected" gate.
2. Preset schema loading: way count, driver roles, expected
   near-field envelopes, crossover regions, safe sweep ranges, delay
   ranges, polarity, gain trims, limiter values, BSC parameters, and
   pass/fail thresholds.
3. Commissioning-safe CamillaDSP profile generation for 2-way and
   3-way templates.
4. Channel identification: quiet band-limited tone per output, with
   DMM/oscilloscope or dummy-load verification before drivers are
   connected, then operator confirmation with low-level band-limited
   tones.
5. Per-driver measurement mode: isolate woofer/mid/tweeter, enforce
   safe sweep range and level, and record active filters.
6. Null-depth delay/polarity search per crossover region.
7. Gated summed-response verification through crossover regions.
8. Measurement import: REW/VituixCAD FRD/IR imports first; REW local
   API integration is plausible later.
9. Provenance in bundles: driver, angle, axis, distance, timing
   reference, mic calibration, gate/window, active profile, sweep
   voltage/SPL, amp gain, output channel map, protection-cap state,
   protective-HP state, smoothing, ZMA/impedance files, and raw FRD /
   IR / capture paths.
10. Crossover candidate compiler: structured crossover/filter/delay/
   gain/limiter data to validated CamillaDSP YAML.
11. Delay/polarity checks: predicted sum, measured sum, inverted-null
   depth, phase tracking, and group-delay plots.
12. Acceptance gates: no "commissioned" label without timing-valid
   driver measurements and at least minimal off-axis validation.
13. Rollback and A/B: accepted speaker baseline, previous baseline,
    room correction bypass, preference EQ bypass.
14. Thermal/level validation: step up in small increments, monitor
    woofer excursion, tweeter distortion, limiter activation, digital
    clipping, and Pi underruns at the intended sample rate/chunk size.

Updated execution plan:

1. **Substrate slice**: implement data models and validation for
   speaker presets, active channel maps, and baseline profiles without
   loading them onto hardware yet. Started 2026-06-01 as
   `jasper.active_speaker`; current scope is validation plus muted
   startup-template generation only, not live DSP loading.
2. **Safe config slice**: generate 2-way and 3-way CamillaDSP
   templates with explicit muted/protected startup state, validate
   them, and make rollback mechanical. Started 2026-06-01 as a
   no-apply startup-template emitter and `jasper-active-speaker
   startup-template` CLI. The CLI writes candidate YAML from preset
   JSON and runs `camilladsp --check` when available. Expanded
   2026-06-03 with `jasper.active_speaker.staging`,
   which binds the saved output topology to the Epique/F110M safe
   bring-up preset and writes a protected startup candidate plus
   evidence metadata without loading CamillaDSP or emitting sound.
   Expanded 2026-06-04 with `jasper.active_speaker.startup_load`, which
   can load that staged startup graph through the shared DSP apply
   lifecycle only after deterministic gates pass, persists the prior
   config as a rollback anchor, and exposes rollback through `/sound/`.
   This is still not a playback slice.
3. **Engineering interop slice**: import REW/VituixCAD measurement
   artifacts and freeze the first named preset before attempting an
   end-user wizard. Started 2026-06-01 with a data-only DE250 +
   E150HE-44 worked-example preset; added the Epique E150HE-44 +
   Eminence F110M-8 safe bring-up preset on 2026-06-03 for Jasper's
   immediate mono cabinet build. Real engineering artifacts, expected
   envelopes, and limiter thresholds are still future work.
4. **Channel and path safety slice**: prove every audible source
   path, including TTS/cues and test tones, flows through the active
   baseline and cannot bypass tweeter protection. Started 2026-06-01
   with `jasper.active_speaker.path_safety` and `jasper-active-speaker
   path-audit`, which encode and evaluate the required evidence but
   do not probe hardware yet. Expanded 2026-06-02 with
   `jasper.active_speaker.environment` and `jasper-active-speaker
   environment-probe`, which inspect ALSA playback devices and the
   current/provided CamillaDSP config without playback, reload, or
   mutation. Manual/operator evidence can pass the checklist, but
   future loading remains blocked until hardware-probe-backed evidence
   exists and the active startup candidate validates. Expanded again with
   `jasper.active_speaker.safe_playback`, which provides no-audio arm/stop
   session bookkeeping for the future tone path without authorizing playback.
   Expanded with `jasper.active_speaker.tone_plan`, which prepares
   preset-derived, clamped channel-test plans while still forbidding playback.
   Expanded with `jasper.active_speaker.playback`, an artifact-first backend
   seam that renders bounded logical-output WAV artifacts and, only with
   explicit lab env enablement, can run the generated artifact through `aplay`
   for woofer, mid, and subwoofer topology targets only.
   Expanded with `jasper.active_speaker.readiness`, a read-only
   playback-readiness gate that evaluates one requested output target across
   safe-session, output topology, channel identity, tweeter protection,
   clock-domain, active-config/path safety, calibration-level, Stop evidence,
   and tone-backend status. It is the contract an audible backend must trust
   before attempting playback. Default installs still return
   `playback_allowed: false`; the lab `aplay` backend can make woofer, mid, and
   subwoofer targets eligible only after protected startup load evidence is
   current.
5. **Consumer W0 slice**: prototype phone-as-mic raw PCM WebSocket
   capture, calibration blocking, browser processing sanity checks,
   and resumable server-side session state.
6. **Consumer W4-W7 slice**: add per-driver near-field checks,
   null-depth delay search, and gated summed verification against the
   preset envelopes.

This deliberately avoids starting with an LLM-guided active wizard.
The first product value is deterministic safety and repeatability.

## LLM Boundary

An LLM advisor can help explain, sequence, and translate:

- explain why a crossover dip is not a room mode;
- ask whether timing reference was used;
- explain null-test results and off-axis concerns;
- suggest which deterministic check to run next;
- generate user-facing summaries and audit-log narration.

The LLM must not:

- emit arbitrary CamillaDSP YAML;
- decide to remove tweeter protection;
- invent limiter thresholds without driver/amp data;
- call magnitude-only data valid for phase alignment;
- silently fold room correction or taste EQ into the speaker baseline.

## Open Questions

- What exact output hardware will the active build use: multi-channel
  USB DAC, HAT, DSP amp board, or separate amp chain?
- Is the target a mono active cabinet with two outputs, a stereo
  active pair with four outputs, or both?
- What exact woofer, tweeter, horn/waveguide, baffle, enclosure, and
  center-to-center spacing will Jasper's first active build use?
- How should TTS/cues be routed so they always pass through the same
  crossover protection as music?
- Which parts should be in-product JTS tooling versus external
  REW/VituixCAD workflow with imports?
- Does the active wizard use the current SciPy/NumPy ESS code path,
  adopt `pyfar`, or wrap both behind one analysis interface?
- How reliable is external USB/Lightning microphone enumeration and
  raw `getUserMedia` capture on current iOS Safari and Android Chrome
  when EC/AGC/noise suppression are disabled?
- Does the deployed CamillaDSP version expose the limiter/filter
  primitives we want, or do we need a compatibility layer?
- What profile schema should represent speaker baseline versus room
  correction versus preference EQ?
- For 3-way speakers with a lower crossover around 250-500 Hz, what
  pass/fail language accurately communicates reduced in-room gating
  confidence without blocking useful commissioning?
- What exact startup sequencing, amp standby/relay behavior, and
  subsonic/rumble high-pass should be mandatory before active output
  is considered safe?

## Failure Modes To Keep Visible

- Full-range signal reaches tweeter due to wrong channel map,
  disabled crossover, daemon crash, or bypass audio path.
- On-axis-only optimization creates vertical lobing/nulls around the
  crossover.
- USB mic measurement without timing reference is used for delay
  alignment.
- Listening-position room correction hides a speaker-baseline
  crossover problem.
- Limiter threshold is treated as thermal/excursion safety when it is
  only a digital peak guard.
- Protection capacitor or horn path changes acoustic response, but
  measurements were taken without the final hardware in place.
- FIR is enabled without latency, CPU, headroom, and pre-ringing
  checks.
- Startup/reboot/USB-clock pops reach amplifiers before CamillaDSP is
  running and protected.
- A user edits only the woofer low-pass or bypasses a filter for
  comparison, accidentally leaving the tweeter full range.
- WebRTC or browser voice processing touches the measurement stream,
  making deconvolution/level data untrustworthy.
- The wrong microphone calibration file is loaded, or no calibration
  file is loaded, and the wizard treats it as a soft warning.
- A 3-way lower crossover in the 250-500 Hz region is judged with an
  indoor gate that cannot support that frequency range.

## Source Reports

This handoff distills three raw research artifacts archived under
[`docs/research/2026-05-25-calibration-agent/`](research/2026-05-25-calibration-agent/README.md):

- [`active-speaker-dsp-commissioning-architecture.md`](research/2026-05-25-calibration-agent/raw/active-speaker-dsp-commissioning-architecture.md)
- [`active-crossover-measurement-workflow.md`](research/2026-05-25-calibration-agent/raw/active-crossover-measurement-workflow.md)
- [`jts-two-way-camilladsp-commissioning-plan.md`](research/2026-05-25-calibration-agent/raw/jts-two-way-camilladsp-commissioning-plan.md)

It also incorporates the 2026-05-26 proposal-v3 methodology supplied
in the working session: generic 2-way/3-way active commissioning,
three-layer DSP separation, near-field/null-depth/gated measurement
triad, preset-first architecture, phone-as-mic raw PCM transport, and
the DE250 + E150HE-44 worked example.

Key external prior-art families named by the reports:

- REW for measurement, timing reference, impulse/phase/group-delay
  inspection, and possible local API integration.
- VituixCAD for crossover simulation, near/far merge, directivity,
  listening-window, and polar validation.
- CamillaDSP for active routing, IIR biquads, FIR convolution,
  gain/delay/polarity, and limiter/compressor primitives.
- Linkwitz/Riley/Vanderkooy/Lipshitz crossover literature for
  non-coincident driver integration.
- rePhase / DRC-FIR and CamillaFIR if verified as later FIR
  references, not first implementation defaults.
- Charlie Hughes / Voice Coil measurement geometry, Purifi and Rod
  Elliott acoustic-center/BSC cautions, Klippel-style protection
  thinking, miniDSP active-cap guidance, Hypex Filter Design UI
  patterns, `pyCamillaDSP`, `camillagui`, `pyCamillaDSP-plot`,
  `wirrunna/CamillaDSP-Building-a-Config`, and
  `mdsimon2/RPi-CamillaDSP`.

Last verified: 2026-06-09
