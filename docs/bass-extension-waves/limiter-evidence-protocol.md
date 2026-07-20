# Bass Extension limiter-evidence protocol

> **Status (2026-07-19): protocol revision `2026-07-19b` frozen; bench result
> not established.**
> This document defines the minimum campaign and pure producer contract needed
> before Wave 4 commissioning may publish a sealed target. It authorizes no
> production caller, hardware playback, or limiter value. Jasper executes the
> later bench pass.

This is the reviewed prerequisite amendment to
[`wave-4-commissioning-backend.md`](wave-4-commissioning-backend.md). The JTS3
crossover substrate burn-in is complete, as recorded in
[`HANDOFF-crossover-measurement-v2.md`](../HANDOFF-crossover-measurement-v2.md).
The remaining result is **not established**: no retained bundle binds a
target-specific limiter setting to the exact downstream sample point, the
active target/candidate graph, replayable digital evidence, and passing
acoustic/protection evidence.

## Claim boundary

An accepted campaign establishes one measured limiter setting for each target
in one exact sealed family. The producer selects values the bench actually
activated and measured. It never calculates a setting from boost, digital
margin, commanded volume, a source-file peak, or an assumed crest factor.

The result does not establish universal loudspeaker safety for every waveform,
spectrum, duration, amplifier, or driver temperature. A finite campaign cannot
make that claim. It establishes the deployed limiter transfer and the recorded
quality/protection verdicts for the named sweep and sustain envelope. Anything
beyond that envelope is **not established**; the smallest evidence that could
broaden it is a separately reviewed protection model and a campaign designed
for the broader envelope.

The existing driver-safety profile, excitation admission, measurement Stop,
and graph restoration gates remain mandatory. A limiter result is not playback
permission or a thermal controller.

## Frozen sample point and units

The observed point is the instantaneous floating-point sample at the input of
the existing per-driver CamillaDSP `Limiter` returned by
`jasper.active_speaker.camilla_yaml.driver_baseline_limiter_name` or
`jasper.active_speaker.camilla_yaml.sub_baseline_limiter_name`, according to
the recorded bass-owner kind.

`jasper.active_speaker.camilla_yaml._assert_bass_extension_safe` owns the
ordered proof: `bass_ext_lt`, then `bass_ext_subsonic`, then the named limiter
on exactly the owner channels. `pipeline_contains_chain` proves membership,
not order, so it is not sufficient by itself. The pre-limiter tap observes the
sample stream after every earlier filter in that exact owner step; the paired
post-limiter tap observes the same channel immediately after the limiter. Both
taps must be non-mutating and content-addressed.

The reference is instantaneous sample-peak dBFS relative to unity
floating-point full scale. It is not acoustic SPL, RMS, loudness, source-file
level, or commanded volume. This limiter has no separate RMS/envelope detector;
“detector point” in the wave charter means this exact per-sample input.

The deployed CamillaDSP build is part of the evidence. Its `soft_clip` path
applies a sample-domain transfer, rather than an onset detector, as shown in
the pinned
[`filters/limiter.rs`](https://github.com/HEnquist/camilladsp/blob/v4.1.3/src/filters/limiter.rs).
The campaign therefore records and replays complete pre/post sample artifacts;
it does not use an undefined `limiter_engaged` predicate.

## What Wave 0 can be reused

The Wave 0 memo at
[`docs/research/2026-07-16-bass-extension-spikes/README.md`](../research/2026-07-16-bass-extension-spikes/README.md)
retains transition, reload-persistence, and harmonic-extraction results. Those
results justify the silent `PatchConfig`/exact-restore bench mechanism and the
harmonic-analysis implementation. They do not contain an admitted bass ladder,
a sustain run, or target-specific limiter observations.

The six facts named by the Wave 4 prerequisite have the following status:

| Fact | Reusable scope | What it fails to establish now |
|---|---|---|
| Sweep | Wave 0 validates the harmonic extractor and narrow-sweep alignment requirements. | No retained admitted target sweep, detector samples, clean ceiling, or limiter-candidate verdict exists. |
| Sustain | No Wave 0 sustain artifact exists. | Spectrum, hold, thermal/port behavior, detector samples, and candidate verdict are not established. |
| Commanded level | Wave 0 retains commands used for transition experiments only. | No settled target-specific ladder level or limiter-input peak is established. |
| Stimulus peak | Wave 0 retains source levels for its transition/harmonic stimuli only. | No post-target, pre-limiter peak for a commissioning stimulus is established. |
| Target boost | Wave 0 confirms Linkwitz-transform mechanics, not a commissioned target family. | No realized detector peak or safe limiter setting follows from boost. |
| Digital clamp | The merged Wave 1 clamp is reusable as arithmetic eligibility code. | It is not a measured driver-safe or transparent limiter setting. |

Accordingly, the new campaign reuses the Wave 0 transition and harmonic
methods, but it must generate fresh target-specific sweep, sustain,
commanded-level, source-peak, boost-identity, and clamp evidence. Any future
retained artifact may replace a fresh pass only when its content identity,
target, active graph, admission receipt, and analysis contract all match.

## Required bench owner — no hidden authority

The current tree has no owner that chooses a detector probe, authors a bass
sustain request, or activates a proposed target/candidate pair. The existing
`resolve_driver_excitation_ceilings` returns a permitted band and maximum peak;
`prepare_driver_excitation_plan` admits or refuses a fully caller-authored
request. Neither chooses the request.

Therefore Jasper must begin with a reviewed `campaign_manifest` produced by
the bench runner. It records, for every target and stimulus role:

- `requested_stimulus_band_hz`
- `requested_stimulus_effective_peak_dbfs`
- `requested_commanded_main_volume_db`
- `requested_hold_duration_s`
- `requested_cooldown_s`
- `requested_repeat_count`
- `stimulus_generator_identity`

Those values are operator-authorized inputs, not outputs invented by this
protocol or by the pure producer. The manifest cites the current confirmed
driver-safety profile and selected `MarginPolicy`. The existing safety planner
then emits the corresponding request, limits, protection, and admission
receipts or refuses. The accepted bundle records both requested and admitted
values. A missing manifest value or admission receipt is **not established**;
the operator must not fill it from a default.

No production bench runner is present in this revision. Before hardware
playback, the runner and its temporary graph activation must receive their own
independent safety review. The pure producer skeleton does not implement either
one.

## Smallest campaign

The campaign covers every target in the exact sealed family, including
natural, because no unmeasured target inherits another target's setting.

### Stimulus set

The roles are exact:

- `digital_transfer_probe`: a deterministic, content-addressed sample program
  rendered through an isolated CamillaDSP file sink. It never reaches hardware.
  The runner records the pre-limiter stream, deployed post-limiter stream, and
  the reference post-limiter stream produced by the pinned implementation.
  Matching payload SHA and byte size between the deployed and reference
  post-limiter artifacts establishes the deployed transfer binding; bundle
  path metadata may differ.
- `sweep_transparency`: the admitted narrow bass sweep and repeats selected by
  the current `MarginPolicy`. It produces settled commanded-level, source-peak,
  pre/post-limiter, acoustic capture, harmonic, tracking, compression, SNR,
  clamp, and quality evidence.
- `sustain_stress`: the admitted deterministic band-limited noise program. Its
  requested hold comes from the selected `MarginPolicy.sustain_duration_s`; its
  admitted hold and cooldown come from the exact prepared excitation plan. It
  produces sag, corner-shift, port, thermal-observation, pre/post-limiter, clamp,
  and quality/protection evidence.

No role may be replaced by music or an operator-selected file. The protocol
contains no stimulus, level, frequency, duration, cooldown, repeat, or limiter
number. Every such quantity is a named manifest, admission, or measurement
output retained in the bundle.

### Discovery pass

For each target, enter `measurement_window()`, snapshot the exact predecessor,
fade to the safe floor, and temporarily load the proposed sealed natural graph.
While silent, apply the target's exact LT/subsonic parameters with the Wave 0
micro-stepped `PatchConfig` mechanism; keep the existing baseline limiter for
discovery. Read back the complete active graph and prove target identity,
ordered owner chain, limiter definition, configured `clip_limit`, and owner
channels before unmuting.

Run the admitted sweep ladder and sustain role. The pre-limiter sample peaks
at or below the read-back baseline limiter setting become the only eligible
candidate inventory. Each candidate retains the exact source
stimulus, admission, pre-limiter PCM, active-graph read-back, and peak-analysis
artifacts. Restore and re-prove the exact predecessor before leaving discovery.

### Candidate pass

Order the distinct measured candidates from least to most permissive. For each
candidate:

1. Enter `measurement_window()`, snapshot the exact predecessor, and fade to
   the safe floor.
2. Load the proposed natural graph, apply the exact target LT/subsonic values,
   and retain the baseline limiter. Read back this reference graph, record a
   `reference_activation_receipt`, and capture the admitted paired-reference
   sweep. Fade to the safe floor again, then apply the candidate to the named
   limiter's `clip_limit`. This is an operator bench mutation; it is not
   `apply_bass_extension` and does not persist a profile.
3. Read back the candidate graph. Record a `candidate_activation_receipt` proving
   the target fingerprint, exact configured candidate, ordered owner chain,
   graph fingerprint, limiter definition/mode, and owner channels. Refuse
   before playback on any mismatch.
4. Run the isolated `digital_transfer_probe`, then the admitted
   `sweep_transparency`, then `sustain_stress`. Use the manifest's exact request
   and the prepared plan's exact admitted hold, cooldown, and repeats.
5. Fade to the safe floor, restore the exact predecessor, and record one
   `candidate_restoration_receipt` proving the restored live graph and state.
6. Stop at the first `accepted` candidate. Advance only after a
   `limiter_transparency_failed` candidate whose transfer, quality, and
   protection verdicts passed, whose separately recorded paired-reference
   transparency verdict failed, and whose predecessor was restored. A refusal,
   abort, quality failure, or protection failure ends the target.

This is the smallest campaign because it performs one discovery activation per
target, one activation/restoration per tested candidate, one hardware-free
transfer render, one admitted sweep set, and one admitted sustain hold. It does
not search for a maximally permissive setting.

## Abort and refusal rules

Abort active playback immediately for operator Stop, audible mechanical
distress, unexpected port noise, amplifier or DAC clipping, outputd or
CamillaDSP fault, graph/read-back mismatch, tap discontinuity, capture clip,
microphone movement, or loss of calibration.

The run also stops on any existing compression, harmonic, tracking, repeat-
spread, SNR, clamp, sag, or corner-shift failure; an admission refusal; a hold
or cooldown mismatch; a missing sample/capture artifact; a transfer mismatch;
or failed predecessor restoration. Preserve partial artifacts with `refused` or
`aborted`. The smallest evidence that clears one is a fresh admitted pass for
the same target, candidate, stimulus, and frozen context.

## Replayable accepted bundle

Every artifact field below is an exact serialized
`jasper.audio_measurement.evidence_identity.ArtifactIdentity`. Bare booleans do
not stand in for measurements. The bundle contains the PCM/capture artifacts
and the analysis artifacts needed for independent replay.

The root has exactly:

- `kind`: `jts_bass_extension_limiter_evidence`
- `schema_version`: the module constant `LIMITER_EVIDENCE_SCHEMA_VERSION`
- `protocol_revision`: the module constant `LIMITER_EVIDENCE_PROTOCOL_REVISION`
- `evidence_fingerprint`: canonical bundle identity
- `measured_context`
- `campaign_manifest`: an `ArtifactIdentity`
- `retained_facts`
- `targets`

`measured_context` and the separately supplied `required_context` each have
exactly:

- `target_family_fingerprint`
- `target_order`, a non-empty list of exact `target_id` and
  `target_fingerprint` objects
- `driver_safety_fingerprint`
- `margin_policy_fingerprint`
- `transparency_policy_fingerprint`
- `natural_graph_fingerprint`
- `baseline_limiter_clip_limit_dbfs`
- `limiter_domain_min_dbfs`
- `limiter_domain_max_dbfs`
- `limiter_domain_fingerprint`
- `camilladsp_build_id`
- `owner_channels`
- `sample_rate_hz`
- `limiter_name`
- `limiter_type`
- `soft_clip`
- `tap_implementation_id`
- `detector_reference`

Every fingerprint is a canonical lowercase SHA content identity.
`target_order` runs deepest target through natural; its objects have exactly
`target_id` and `target_fingerprint`. `owner_channels` is a non-empty ordered
list of unique non-negative exact integers, and `sample_rate_hz` is a positive
exact integer. Build, limiter, tap, and target identifiers are non-empty
trimmed strings. `limiter_type` is exactly `Limiter`, `soft_clip` is exactly
true, and unknown context fields are inconsistent.
`baseline_limiter_clip_limit_dbfs` is the finite read-back value from the
existing limiter and is the most-permissive candidate the protocol accepts.
`limiter_domain_min_dbfs`, `limiter_domain_max_dbfs`, and
`limiter_domain_fingerprint` are trusted outputs of a reviewed context builder
bound to the current `emit_active_speaker_baseline_config` limiter-range
validation. They are not manual manifest values. The domain endpoints are
finite and increasing, and the baseline setting lies inside the closed domain.
`transparency_policy_fingerprint` binds the exact paired-reference analysis
policy authorized in `campaign_manifest`; an absent field is missing, while a
fully valid measured/required mismatch is stale. Neither case permits inventing
a comparison threshold.

The detector reference is exactly
`instantaneous_float_sample_peak_dbfs_re_unity_at_limiter_input`.

`retained_facts` has exactly `sweep`, `sustain`, `commanded_level`,
`stimulus_peak`, `boost`, and `digital_clamp`. Each value has `status` exactly
`replaced` and `artifact`, whose value is one `ArtifactIdentity`. A future reuse
authority requires a contract amendment that can bind the retained identity to
trusted current context; this revision does not accept self-asserted reuse.

Each target has exactly:

- `target_id`
- `target_fingerprint`
- `result`

`result` is a strict discriminated union. An early stop has exactly
`disposition` (`refused` or `aborted`), `stop_receipt` (an `ArtifactIdentity`),
and `partial_artifacts` (a possibly empty list of `ArtifactIdentity` values).
It is semantically `out_of_envelope`, never `missing` merely because later
campaign phases did not run.

An evaluated result has exactly:

- `disposition`: `evaluated`
- `discovery_activation_receipt`: an `ArtifactIdentity`
- `candidate_sources`, a non-empty list of source observations
- `discovery_restoration_receipt`: an `ArtifactIdentity`
- `candidates_least_to_most_permissive`, a non-empty list of complete candidate
  records

Each source observation has exactly:

- `source_fingerprint`
- `stimulus`: an `ArtifactIdentity`
- `admission`: an `ArtifactIdentity`
- `active_graph_readback`: an `ArtifactIdentity`
- `pre_limiter_pcm`: an `ArtifactIdentity`
- `peak_analysis`: an `ArtifactIdentity`
- `pre_limiter_peak_dbfs`

`source_fingerprint` is
`jasper.audio_measurement.evidence_identity.json_fingerprint` over that source
observation without `source_fingerprint`.

Each candidate has exactly:

- `limiter_threshold_dbfs`
- `source_fingerprint`, naming one source observation whose recorded peak is
  exactly the candidate setting
- `candidate_activation_receipt`: an `ArtifactIdentity`
- `configured_clip_limit_dbfs`, exactly the candidate setting read back from
  the named limiter
- `active_target_fingerprint`
- `active_graph_fingerprint`
- `ordered_owner_chain`
- `digital_transfer_probe`
- `sweep_transparency`
- `sustain_stress`
- `candidate_restoration_receipt`: an `ArtifactIdentity`
- `restored_graph_fingerprint`, exactly the context's natural graph
- `disposition`: `accepted` or `limiter_transparency_failed`

The digital transfer record has exactly `stimulus`, `pre_limiter_pcm`,
`post_limiter_pcm`, `reference_post_limiter_pcm`, `transfer_analysis` (all
`ArtifactIdentity`), and `verdict` (`pass` or `fail`). The transfer analysis
must be replayable from those sample artifacts; the producer consumes its
content identity and verdict but does not manufacture the analysis.

The sweep and sustain records each have exactly:

- `stimulus`, `admission`, `pre_limiter_pcm`, `post_limiter_pcm`,
  `acoustic_capture`, `signal_analysis`, and `protection_analysis`, all
  `ArtifactIdentity`
- `stimulus_band_hz`
- `stimulus_effective_peak_dbfs`
- `commanded_main_volume_db`
- `target_boost_db`
- `digital_clamp_passed`
- `pre_limiter_peak_dbfs`
- `post_limiter_peak_dbfs`
- `hold_duration_s`
- `required_cooldown_s`
- `repeat_count`
- `quality_verdict` and `protection_verdict`, each `pass` or `fail`

The sweep record additionally has `reference_activation_receipt`,
`reference_stimulus`, `reference_admission`, `reference_acoustic_capture`, and
`transparency_analysis`, all `ArtifactIdentity`; it also has
`reference_target_fingerprint`, `reference_active_graph_fingerprint`,
`reference_configured_clip_limit_dbfs`, and `transparency_verdict` (`pass` or
`fail`). The reference target must equal the candidate target, the reference
configured limiter must equal the context's baseline limiter, and the reference
activation receipt must prove that target/graph/limiter read-back. Reference and
candidate stimulus and admission payloads must match by SHA and byte size. The
analysis artifact owns the paired comparison and is independently replayable.

Bands are arrays of exactly two finite positive floats in increasing order.
Peak, level, boost, and limiter fields are finite floats; hold durations are
positive finite floats; cooldowns are non-negative finite floats; repeat counts
are positive exact integers; and `digital_clamp_passed` is an exact bool.
Every source peak, candidate setting, candidate configured read-back, and
reference configured read-back must lie inside the trusted closed limiter
domain. The context builder is not present in this revision, so production
wiring remains blocked rather than reconstructing the emitter's domain here.

An `accepted` candidate requires a transfer pass, both quality and protection
passes, a transparency pass, `digital_clamp_passed` true in both hardware
records, exact activation/source/restoration bindings, and all artifacts. A
`limiter_transparency_failed` candidate requires transfer, quality, protection,
and clamp passes, a transparency fail, exact restoration, and no abort/refusal.
Any contradictory disposition is inconsistent. A runner that encounters a
transfer, quality, protection, or clamp failure must end that target as
`refused` or `aborted` and retain the stop receipt and partial artifacts; it
must not emit an evaluated candidate for that failed pass. Encoding the target
as `evaluated` with either candidate disposition while one of those required
verdicts failed is `inconsistent`, not `out_of_envelope`.

`ordered_owner_chain` is a non-empty list of unique non-empty filter names. It
must contain `bass_ext_lt`, then `bass_ext_subsonic`, then the context's
`limiter_name` in that relative order. Extra existing filters are allowed only
when retained in their exact read-back order.

Every target must yield one accepted candidate. The producer selects the first
accepted candidate after zero or more transparency failures. A deeper target
may not receive a more-permissive setting than a shallower target. A violation
is out of envelope; the producer does not search, interpolate, or substitute.
Candidate settings are strictly increasing in stored order. Selected settings
are non-decreasing from deepest target through natural. Every candidate and
its configured read-back must be no more permissive than
`baseline_limiter_clip_limit_dbfs`; every candidate-source peak must also be at
or below that ceiling. A violation is out of envelope even when that source is
not selected. A value outside the trusted emitter domain is also out of
envelope.

## Pure producer contract

The exact entry point is:

`jasper.bass_extension.limiter_evidence.produce_limiter_thresholds(evidence,
*, required_context)`

Both inputs accept arbitrary objects. `required_context` is supplied through a
separate trusted boundary by a future production caller; it is never read from
the stored bundle. The function is pure, performs no I/O, and returns exactly
`LimiterThresholdSet` or `LimiterEvidenceRefusal`. The same two inputs produce
the same serialized result.

The module freezes these values and types:

- `LIMITER_EVIDENCE_SCHEMA_VERSION = 1`
- `LIMITER_EVIDENCE_PROTOCOL_REVISION = "2026-07-19b"`
- `LimiterRefusalReason`: `missing`, `stale`, `inconsistent`,
  `out_of_envelope`
- frozen `TargetLimiterThreshold(target_id, target_fingerprint,
  limiter_threshold_dbfs, source_fingerprint)`
- frozen `LimiterThresholdSet(evidence_fingerprint,
  required_context_fingerprint, targets)`
- frozen `LimiterEvidenceRefusal(reason, evidence_paths)`

`LimiterThresholdSet.to_dict()` emits exactly `schema_version`, `kind` equal to
`jts_bass_extension_limiter_threshold_set`, `evidence_fingerprint`,
`required_context_fingerprint`, and ordered `targets`; each target emits the
four fields named by `TargetLimiterThreshold`. `LimiterEvidenceRefusal.to_dict()`
emits exactly `schema_version`, `kind` equal to
`jts_bass_extension_limiter_evidence_refusal`, `reason`, and
`evidence_paths`.

The input domain is strict JSON: objects have string keys; arrays preserve
order; scalar values are null, bool, exact int, finite float, or string.
Unknown fields are inconsistent. Fingerprints are canonical lowercase SHA
content identities. `evidence_fingerprint` is
`jasper.audio_measurement.evidence_identity.json_fingerprint` over the root
without `evidence_fingerprint`; the required-context fingerprint uses the same
helper over the validated context.

A non-object top-level input is `inconsistent` at `$evidence` or
`$required_context`. An object missing fields is `missing` at each absent path;
an empty required collection is `missing` at that collection's path unless the
schema explicitly permits emptiness; a present value of the wrong type is
`inconsistent` at its path. Fully valid context inequality is the only `stale`
condition.

Evidence paths use virtual roots `$evidence` and `$required_context`,
dot-separated object fields, and bracketed array indices. A stale mismatch
reports both the measured and required paths. The function collects every
defect without raising, selects the first non-empty category in this precedence,
and returns only that category's sorted, duplicate-free paths:

1. `missing`: an absent required field or empty required collection.
2. `stale`: the fully valid `measured_context` differs from the fully valid
   trusted `required_context`.
3. `inconsistent`: unsupported kind/schema/revision; unknown field; wrong JSON
   type; malformed identity/artifact; non-finite quantity; fingerprint mismatch;
   duplicate target/source/candidate; target order mismatch; candidates not
   strictly least-to-most permissive; source peak, configured limiter, active
   target, ordered chain, graph, restoration, or disposition contradiction.
4. `out_of_envelope`: target-level refusal/abort; no accepted candidate after
   one or more valid transparency failures; candidate or source above the
   baseline limiter ceiling; limiter value outside the trusted emitter domain;
   accepted evidence after the first accepted candidate; or conservative
   family-order violation.

Malformed top-level inputs refuse at their virtual root. No evidence content
causes an exception, default value, clock read, hardware query, mutation,
interpolation, or guessed setting.

## Amendment history

- **`2026-07-19b`** — makes the candidate/result boundary explicit. Honest
  transfer, quality, protection, or clamp failures stop the target and use its
  `refused`/`aborted` result arm. An evaluated candidate carrying one of those
  failures contradicts both allowed candidate dispositions and is
  `inconsistent`. This resolves the former precedence wording that also listed
  the same contradictory candidate as `out_of_envelope`.
- **`2026-07-19`** — initial frozen bench campaign, evidence schema, pure
  producer, and total-refusal contract.

## Pre-production fence

Only `jasper/bass_extension/limiter_evidence.py` and
`tests/test_bass_extension_limiter_evidence.py` may implement the pure skeleton
from this revision. Synthetic bundles must exercise every refusal category,
precedence, sorted paths, strict schema, artifact bindings, source/activation/
restoration contradictions, family ordering, `to_dict()` shapes, and
round-trip determinism.

The module must have no import or call from `jasper.bass_extension.__init__`,
`apply_bass_extension`, a graph emitter, daemon, web backend, or other
production path. Synthetic values prove parser/refusal behavior only; they are
not limiter evidence.

Wave 4 production remains blocked until Jasper produces a real accepted bundle,
the bundle and replay pass independent review at zero Blockers and zero
Should-fixes, and a later Wave 4 contract revision names its exact fingerprint
and authorizes a trusted caller. Until then, a sealed target's
`limiter_threshold_dbfs` is **not established**.
