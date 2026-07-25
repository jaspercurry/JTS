# Bass Extension limiter taps — offline-render realization (reviewed amendment)

> Reviewed amendment — **accepted by the maintainer 2026-07-24**, after six
> revisions and five independent adversarial rounds reaching zero Blockers /
> zero Should-fixes (per [`adversarial-review.md`](adversarial-review.md)).
> It sits beside
> [`limiter-bench-runner-protocol.md`](limiter-bench-runner-protocol.md) and
> [`limiter-bench-runner-activation.md`](limiter-bench-runner-activation.md),
> cross-referenced by file name. Per its own Registration rule this document
> carries no status banner: current program status lives solely in the plan's
> Wave status table
> ([`HANDOFF-bass-extension-plan.md`](../HANDOFF-bass-extension-plan.md)).

## Why this amendment exists (the stop-and-report)

[`limiter-evidence-protocol.md`](limiter-evidence-protocol.md) "Frozen sample
point and units" requires, for every stimulus role, the instantaneous
floating-point samples at the input of the named per-driver CamillaDSP
`Limiter` and immediately after it. Two realization sentences govern the taps:
the frozen protocol's "Both taps must be non-mutating and content-addressed,"
and the runner protocol's "non-mutating, content-addressed reads of the exact
sample stream at the limiter input and immediately after it." The protocol
realizes the mechanism concretely only for `digital_transfer_probe` ("rendered
through an isolated CamillaDSP file sink"). For `sweep_transparency` and
`sustain_stress` — the roles that play through hardware — the tap realization
was left unspecified, and the runner CLI ships fail-closed for exactly this
reason (`jasper/cli/bass_extension_bench.py::_run_live`: "the one piece with
no in-tree helper to compose").

A live tap is not realizable. The second bullet, and the second clause of the
first, are properties of the installed CamillaDSP build, recorded with source
citations at executor-PR time, per R3 and R10(a); the remainder are verified
directly against this tree:

- `jasper.camilla.CamillaController` — the JTS wrapper — exposes only
  volume/mute, playback RMS/peak, clipped-sample count, runtime status, and
  config get/set/patch/reload; the pinned CamillaDSP v4.1.3 websocket API
  likewise exposes no interior-stage PCM read.
- CamillaDSP drives **one playback device per process**; it cannot feed the
  DAC and write an inline file sink of an interior pipeline stage in the same
  pass.
- Every capture path in the product (chip mic, USB, phone relay) observes
  acoustics or renderer input — none observes an interior CamillaDSP stage.
- The post-DSP snd-aloop lane that `jasper-outputd` reads does carry the
  digital post-pipeline stream, but it is `S16_LE` and post-fader, so it
  cannot deliver the frozen "instantaneous floating-point sample at the
  limiter input/output" and is not a realization of either tap.

Per the runner amendment's own rule ("a need discovered mid-implementation
that the protocol does not cover is a stop-and-report, not an improvisation"),
this document is that report plus the proposed contract resolution.

## The realization in one paragraph

The pre- and post-limiter tap artifacts for **all three stimulus roles** are
produced by **deterministic offline re-renders through the exact deployed
`camilladsp` binary** (file capture → file playback, no audio hardware), using
**derived configs** that are proof-carrying truncations of the *proved live
graph* for that pass. The acoustic capture remains the live measurement; the
tap artifacts are the digital-domain evidence, rendered from the same
content-addressed stimulus the admitted pass played, under proofs that bind
the render to the live pass (R1–R10 below), including one rule — R10 — that
compares a rendered artifact against a live observable and refuses on
disagreement. This is the same mechanism the frozen protocol already mandates
for `digital_transfer_probe`; the amendment extends it to the other two roles
and pins the identity rules the protocol deferred.

## Rules

**R1 — Derivation source of truth; receipt binding.** The only input to
config derivation is the exact `active_config_raw` text proved by the pass's
read-back receipt (already contracted; the runner records it per activation).
Derivation never reads a config file from disk and never re-fetches from a
live daemon. Each artifact names its source receipt: discovery artifacts
derive from the discovery activation read-back; a candidate's
`pre_limiter_pcm` / `post_limiter_pcm` derive from that candidate's
activation read-back. The paired reference activation produces no tap
artifacts — the frozen sweep record has no reference PCM fields.

**R2 — Truncation within the single owner step, prefix-proved.** The
per-driver limiter is a filter *name* inside one owner pipeline step, not a
pipeline step (`_assert_bass_extension_safe` in
`jasper/active_speaker/camilla_yaml.py` selects the single step whose
`channels` equal the recorded `owner_channels` and proves name order within
it — the MODEL for this invariant, not what derivation calls: it hard-codes
`view_from_emitted_text` and the baseline `clip_limit`, so it rejects every
candidate graph by construction. The callable predicate derivation itself
uses is `jasper.bass_extension.bench.activation._prove_active_graph`, which
takes an arbitrary config mapping and a parameterized `ActivationProof`).
`_prove_active_graph` applies ONLY to the post-limiter derived config: it
structurally requires the context's `limiter_name` to be present in the
owner step's `names` and ordered after `bass_ext_lt`/`bass_ext_subsonic`, so
it structurally rejects the pre-limiter config, which omits the limiter by
construction. The pre-limiter config is re-proved by a DISTINCT prefix
predicate over the retained prefix: `bass_ext_lt` before `bass_ext_subsonic`,
both on exactly the owner channels, the limiter name ABSENT, and the retained
prefix an exact prefix of the live owner step's `names`. The executor PR
implements that prefix predicate in its own module; it does not modify
`activation.py`.
Derivation operates on the **raw mapping** — `yaml.safe_load` of the proved
`active_config_raw`, edited in place and re-serialized — never on a
`GraphView`. `view_from_camilla_dict` is used ONLY to re-prove owner-step
invariants against the derived output, the same proof `_prove_active_graph`
already implements; it is never the object derivation parses or edits,
because it is a lossy projection: it drops every non-`Filter` pipeline step
(`Mixer`, `Processor`, …), the per-step `bypassed` flag, the `mixers` block,
and the `devices` block — and the live graphs contain a real signal-affecting
split mixer (`MONO_SUM_GAIN_DB` = −6.02 dB per source,
`jasper/camilla_emit.py`) whose silent omission would ship a limiter 6 dB too
permissive. `view_from_emitted_text` is never used either — it parses JTS's
own emitted YAML text, a different dialect from CamillaDSP's read-back.

The prefix proof is three-part, over the raw mapping: (i) the derived `devices`
block differs from the live block only by R3's capture/playback device swap and
R3's closed normalization allowlist, and by nothing else; (ii) `filters` and
`mixers` are identical after canonical re-serialization, including every mixer
mapping source's `channel`/`gain`/`inverted` — this is where an altered or
dropped split-mixer source (the −6.02 dB hazard above) would be caught; (iii)
the derived `pipeline` equals the live `pipeline` truncated at the owner step,
element-for-element and key-for-key (`type`, `name`, `channels`/`channel`,
`names`, `bypassed`), with the owner step's `names` an exact prefix of the live
owner step's `names` ending at the required boundary. A parse that does not
yield exactly one owner step, or any difference outside (i)-(iii), refuses.

The two boundaries are exact: let `i` be the index of the context's
`limiter_name` within the live owner step's `names`. The post-limiter
derivation retains `names[:i+1]`; the pre-limiter derivation retains
`names[:i]` — the frozen protocol's "after every earlier filter in that
exact owner step." Any other retained length refuses. Both lengths and the
full live `names` list are recorded in the derivation receipt.

**R3 — Device swap with a closed normalization allowlist.** Beyond the
capture/playback device swap, derivation may change exactly these device
keys, each recorded in the derivation receipt with its live value and its
derived value: `enable_rate_adjust` → `false` (a file backend has no output
clock to steer — the same pairing the in-tree file-sink emitter enforces in
`jasper/active_speaker/camilla_yaml.py::emit_active_speaker_program_bake_config`)
and `devices.playback.format` → the derivation receipt's recorded processing
precision. `samplerate`, `chunksize`, `queuelimit`, `target_level`, channel
counts, `volume_limit: 0.0`, and every filter/mixer/pipeline key are
unchanged — `queuelimit`/`target_level` are recorded in the derivation
receipt (live and derived, identical) even though unchanged, settled by
Revision 7's errata below (`Devices.queuelimit`/`Devices.target_level`,
`src/config/mod.rs`, both `Option`, defaults 4 and `chunksize`; no
file-device-specific requirement is documented — the "only changed if the
capture device can provide data faster than playback can consume it"
guidance is about real-time device pacing, meaningless for a batch pass with
no live clock to steer). Any other key difference refuses. Capture uses
CamillaDSP's `WavFile` device type (`src/config/mod.rs`:
`CaptureDevice::WavFile(CaptureDeviceWavFile)`) so the stimulus artifact's
header is never interpreted as audio; a headerless artifact is refused
rather than fed to a raw `File` device. The capture swap sets
`devices.capture.type` to `WavFile` plus its `filename`, and DELETES the
live `device`, `format`, AND `channels` keys entirely —
`CaptureDeviceWavFile` (`#[serde(deny_unknown_fields)]`) declares no
`channels` field at all, settled by Revision 7's errata below; a `WavFile`
capture takes its geometry, including channel count, from the artifact's
own header, not from config. The live `devices.capture.channels` value is
retained only as a validation input (compared against the artifact header
before any render starts) and recorded in the derivation receipt — never
emitted as a derived config key. The artifact header's rate and channel
count must equal `devices.samplerate` and the live `devices.capture.channels`
value, and its sample format must be 16-bit PCM per R6a(iv) — the live
`format: S32_LE` on `plug:jasper_capture` is an ALSA-plug widening of
fan-in's S16 mix, not independent geometry, so a 16-bit artifact is the
bit-exact equivalent. Any mismatch refuses before the render starts. The
playback swap sets `type`/`filename` to the bundle sink and deletes the live
`device` key; it sets `format` to the derivation receipt's recorded
processing precision — **`devices.playback.format` is ADDED to R3's
normalization allowlist**, recorded both live and derived — and KEEPS
`devices.playback.channels` at the live pipeline width:
`PlaybackDevice::File` (unlike `CaptureDeviceWavFile`) declares a required
`channels` field. The playback file carries the full pipeline channel
width, and the derivation receipt records the owner channel's index within
the interleaved frame; the owner channel is extracted offline — no mixer or
pipeline step is ever inserted. Rendered tap artifacts are written in the
derivation receipt's recorded processing precision — the deployed build's
actual CamillaDSP processing precision, settled by Revision 7's errata below
as `F64_LE` for the pinned `deploy/install.sh` build (never the retired
v1/v2 spelling `FLOAT64LE`, which this build's `#[serde(deny_unknown_fields)]`
parser rejects outright) — and the reference post-limiter implementation
computes in that same precision; the frozen transfer verdict is a byte-exact
compare and admits no tolerance.

**R3, continued — analysis window and owner-channel selection.**
`pre_limiter_peak_dbfs` and
`post_limiter_peak_dbfs` are maxima over the STIMULUS BODY ONLY — the frame
range excluding R6's embedded lead-in/lead-out, with the excluded frame
counts recorded — because including the lead-in lets a settling transient
inflate the peak and ship a more permissive limiter. When `owner_channels`
has more than one entry, EACH owner channel yields its own source
observation and its own candidate: per-channel index and peak are recorded,
and each observation's PCM is that one channel's extracted stream. The
campaign never collapses owner channels into one number; a future
single-number revision must take the MINIMUM across owner channels — the
conservative choice — never an unstated one. Per-channel observations enter
the runner's existing distinct-peak inventory unchanged (`runner.py`'s
`distinct.setdefault`) — equal peaks across owner channels collapse to one
candidate per the frozen "distinct measured candidates" and
strictly-increasing rule; this amendment does not change that dedup. What it
forbids is collapsing owner channels BEFORE the inventory.

**R4 — Gain-path reproduction, receipted.** (a) The runner reads the main
fader and mute immediately before and immediately after each stimulus role's
playback — at the locked measurement level, not at the safe floor — and a
mismatch ends that target through the `refused` arm with its partial
artifacts preserved; mute must be false. (b) The deployed build's main-fader
application point is determined from the pinned CamillaDSP source for the
recorded `--version` and is recorded verbatim in the derivation receipt
(file, symbol, and whether it precedes or follows the owner limiter) —
settled by Revision 7's errata below: `src/pipeline.rs`
`Pipeline::process_chunk` runs `self.volume.process_chunk(&mut chunk)`
(built in `Pipeline::from_config` from `processing_params.current_volume(0)`)
BEFORE the `for step in &mut self.steps` loop, unconditionally, for every
configured pipeline step. (c) When the application point precedes the
limiter, the derived configs reproduce the recorded fader gain there; when
it follows the limiter, the derived configs apply no fader gain at all. The
contractual obligation is a bit-identical gain path at the limiter input;
the receipt is what makes it reviewable. For the pinned v4.1.3 build this
resolves PERMANENTLY to the "precedes the limiter" branch — the
process-wide main fader always runs before every pipeline step, so it
always precedes the owner limiter regardless of graph shape; the "follows
the limiter" branch is dead code for this CamillaDSP version, not merely
unreachable on JTS-emitted graphs today. Every render therefore reproduces
the recorded fader gain via `--gain <fader_db>` (`src/bin.rs`:
`Arg::new("gain")` sets `initial_volumes[0]` — the same index
`Pipeline::from_config` reads for `self.volume` — and
`filters/basicfilters.rs`'s `Volume::new` starts with `ramp_step: 0`, so the
first processed chunk already applies it, no startup ramp). (d) A recorded
`--version` other than v4.1.3 — the version `deploy/install.sh`
(`CAMILLA_VERSION`) installs, and the build whose `filters/limiter.rs`
`limiter-evidence-protocol.md` cites for the `soft_clip` transfer — refuses.

**R5 — Binary identity, resolved from the running unit.** The render binary is
resolved from the running `jasper-camilla.service` `ExecStart` and must be the
same file the live graph is executing. `JASPER_CAMILLADSP_BIN` is ignored for
renders, and a set-but-different `JASPER_CAMILLADSP_BIN` refuses the campaign
rather than being silently preferred. The recorded tuple is path + `camilladsp
--version` output + SHA-256 of the binary file, and the recorded version must
equal v4.1.3 — the version `deploy/install.sh` (`CAMILLA_VERSION`) installs,
and the build whose `filters/limiter.rs` `limiter-evidence-protocol.md` cites
for the `soft_clip` transfer — or the campaign refuses. The render is invoked
with the same bounded-subprocess shape `jasper/dsp_apply.py` already uses for
`camilladsp --check` (explicit argv, timeout, captured stdout/stderr, typed
result) — but that resolution and invocation are the runner's own code;
`jasper/dsp_apply.py` is neither imported nor modified by the executor PR.

**R6 — Input identity with runner-owned padding.** Renders consume the exact
content-addressed admitted stimulus artifact the live pass played — the same
bytes, same SHA. Stimulus artifacts MUST embed their lead-in and lead-out
silence inside the artifact (not synthesized at play time), so live playback
and offline renders share one input identity. Padding is produced by the
bench runner's own stimulus assembly, which prepends and appends silence to
the artifact emitted by the existing generators **without modifying them**.
`stimulus_generator_identity` keeps its frozen manifest meaning (the
operator-authorized generator identity per `limiter-evidence-protocol.md`)
and is not redefined here; a separate recorded field,
`padded_stimulus_assembly_identity`, identifies the padded assembly the
render actually consumes. The lead-in is at least ten time constants of the
slowest IIR stage on the owner path at the recorded sample rate; when any
`Conv` stage is on the owner path, the lead-in is also at least the length of
the longest such impulse response. Both computed minima are recorded in the
derivation receipt — the effective lead-in is their maximum. The lead-out is at
least the owner path's total group delay (summed `Delay` stages plus the
longest `Conv` impulse response), also recorded. Modifying
`jasper/audio_measurement/playback.py` or `sweep.py` remains a stop-and-report.

**R6a — Ingress transparency (live side of input identity).** The stimulus
artifact is the render's input only if the live ingress path from the artifact
to CamillaDSP's capture device is proved bit-transparent for the pass,
established by four elements recorded at pass start and end: element (i) from a
mux STATUS read, elements (ii)-(iii) from a fan-in STATUS read, and element
(iv) from the artifact header checked against fan-in's STATUS `sample_rate`
plus the compile-time `CHANNELS`/`FORMAT` constants cited below:

(i) **Isolation.** Mux STATUS reports `test_source == active_source ==` the
measurement fan-in label and `test_owner ==` the bench gate owner for the
whole pass (the `_measurement_gate_held` predicate,
`jasper/correction/coordinator.py`). This is a selected-input gate, not a
per-lane mute: other lanes legitimately remain `muted:false` and MUST NOT be
required muted.

(ii) **Lane state.** The measurement lane reports `muted:false`, no
`resampler` block present (or `resampler.armed:false`), `trim.pending:false`,
and its `xrun_count` / `catchup_resync_frames` / `catchup_events` unchanged
between the pass-start and pass-end reads.

(iii) **No foreign audio at the mix.** Fan-in STATUS must report
`tts.program_duck_active:false` AND `tts.pending_frames:0` at both the pass
start and end reads, and `tts.flushed_frames` / `tts.dropped_audio_frames`
unchanged between them — `program_duck_active` alone is insufficient: a
standalone cue ducks at `cue_duck_gain` and mixes its own audio into the
program lane WITHOUT setting that flag (`rust/jasper-fanin/src/tts.rs`'s
`prepare_period` / `program_duck_gain`). The window's `MEASURE_PAUSE` voice
lease is the MECHANISM that keeps the WakeLoop off the mic during the pass,
but it is NOT an assertable element here: `measurement_window()` yields no
handle to it, proceeds with only a warning on a `MEASURE_PAUSE` failure, and
latches no voice-lease error the runner could observe — the runner protocol
forbids modifying `measurement_window()` to change that. This amendment
claims only the fan-in counter evidence above. Mux-gate isolation (element
(i)) is different: it IS lease-backed and observable — a gate-lease renewal
failure cancels the in-flight play task, which the pass observes as a
cancellation and refuses.

(iv) **Bit-width/geometry.** Fan-in narrows every input to S16 at the mix
(`rust/jasper-fanin/src/mixer.rs`'s module docstring, "Inputs are S16_LE
interleaved stereo," and its `pub const FORMAT: Format = Format::S16LE` /
`pub const CHANNELS: u32 = 2`),
so the stimulus artifact MUST be 16-bit PCM at the lane's configured rate
and channel count; a wider or differently-clocked artifact refuses.

Fan-in exposes no per-lane gain knob, so "unity lane gain" is not an
assertable element — transparency rests on (i)-(iv). Any unproved element
refuses the pass; absent this proof the render's input identity is not
established and no candidate may be derived from it.

**R7 — Owner-path stage allowlist.** The **owner path** is every pipeline
step retained by R2 whose channel set intersects the channels reaching the
owner channel — computed by walking the retained mixers' mappings backwards
from the owner channel to the capture channels — plus every filter named by
such a step and every retained `Mixer` step. Derivation admits only stage
types whose offline behavior is exactly reproducible from the config: Biquad
/ BiquadCombo, Conv, Delay, Gain, Limiter, and Mixer. Enforced against the
raw mapping: any retained step whose type is neither `Filter` nor `Mixer`,
any step carrying `bypassed`, and any filter type outside {Biquad,
BiquadCombo, Conv, Delay, Gain, Limiter} refuses — including but not limited
to `Volume`, `Loudness`, `Dither`, `Processor`, and any Async resampler
(`devices.resampler` block of any Async type), whether or not this document
names it. The refusal is surfaced to the operator; there is no fallback.
`enable_rate_adjust` in the live config is NOT a refusal trigger; it steers
the capture device's clock and does not transform sample values — it is
normalized to `false` per R3.

**R8 — Determinism receipts, per config shape.** A **shape** is the exact
byte content of a derived config, keyed by its SHA-256. A determinism receipt
is rendered for EVERY distinct shape the campaign produces — there is no
per-target floor; an unreceipted shape is an unadmitted artifact. Each is
rendered twice and must be SHA-identical; a mismatch refuses (the determinism
assumption underlying the tap artifacts has been falsified on this host —
that is evidence, not noise). The receipt establishes determinism only for
the shape it covers; it makes no claim about untested shapes. The
pre-limiter render is `clip_limit`-independent in the AUDIO domain — the
truncated pipeline never reaches the Limiter filter — but NOT byte-identical
across candidates: R2(ii) retains the live `filters` block, including the
candidate's `clip_limit` value, verbatim. Each candidate therefore derives a
distinct shape, receipted per candidate; no render is reused across shapes.

**R9 — Execution contract: isolation, ordering, bounds.** Renders are invoked
with no `--statefile`, no `-p`/`-a` websocket bind, an explicit wall-clock
timeout, and write only inside the bundle directory; the invocation argv is
recorded in the derivation receipt, and any render invocation carrying a
statefile or a port refuses before it starts — "non-mutating" means exactly
this argv contract plus the file-only device set. Sweep and sustain renders
run AFTER their acoustic pass and AFTER `measurement_window()` has exited;
the `digital_transfer_probe` render stays in its frozen candidate-pass
position (step 4) — INSIDE the window and the live activation, at the safe
floor, touching no audio device — the single deliberate exception to the
outside-the-window rule, preserving frozen ordering rather than amending it.
Bounds are PROCESS-LOCAL, not systemd: each render is a direct subprocess
child with an explicit wall-clock timeout, `RLIMIT_AS` and `RLIMIT_CPU` set,
and `os.nice()` raised, using the `jasper/dsp_apply.py` `--check` bounded-
subprocess shape (explicit argv, timeout, captured stdout/stderr, typed
result) tightened with the two rlimits. The timeout, the two rlimit values,
and the nice value are recorded campaign-manifest inputs, not unstated
constants. The runner does NOT invoke
`systemd-run`: `scripts/pi-run-diagnostic.sh` is a laptop-side SSH wrapper, a
transient unit is forbidden by the runner protocol's no-new-unit rule
(`limiter-bench-runner-protocol.md`: "it adds no daemon, socket, HTTP route,
timer, or unit"), and its 10-minute runtime ceiling would kill a campaign
mid-activation. The campaign records a per-render byte estimate before the
first render; before each render it refuses if the bundle directory's free
space is below (estimate × renders still outstanding for the campaign).

**R10 — Live cross-check (fail-closed).** (a) The cross-check reads
`jasper.camilla.CamillaController.get_playback_peak_all()` (#1735) — every
playback channel's peak, in channel order, with no truncation and no
mirroring; a per-element `None` reads as `-inf`, and an empty or no-data
reading returns `[]`, distinguishable from any real reading. Whether the
reported peaks meter before or after the main fader is itself a RECORDED
OBLIGATION on the R4(b) pattern: file, symbol, and the resulting sign of
R10(b)'s offset, all recorded in the derivation receipt — settled by
Revision 7's errata below: `rust/…/alsa_backend/device.rs`'s playback loop
runs `chunk.update_stats(&mut chunk_stats)` on the chunk received from the
processing thread, i.e. AFTER the full `pipeline.process_chunk` (fader plus
every step) already ran, so `get_playback_peak_all()` always reads
downstream of the fader. Combined with R4(c)'s permanent "precedes the
limiter" resolution — the render also always carries the fader gain — both
sides of R10(b)'s comparison carry the SAME fader attenuation, so the
offset is always 0 dB for this build; R10(b)'s `−recorded_main_volume_db`
term below is retained as the general (never-taken, for this build) branch,
not deleted, so a reviewer can audit the reasoning without trusting a
hardcoded constant. This is the amendment's most safety-load-bearing
citation; it is no longer VOID. `get_playback_peak_all()`
returning `[]`, or a list shorter than `max(owner_channels)+1`, makes the
cross-check unavailable: the runner records `cross_check: unavailable`,
naming the owner channels involved, and REFUSES the pass — a render is
never admitted uncross-checked. Admissibility is now a per-entry index
check, not a topology restriction: every entry of the recorded
`owner_channels` must be a valid playback channel index — `0 ≤ index <
devices.playback.channels` as recorded in the derivation receipt (R3 keeps
that count equal to the live pipeline width). Multi-entry `owner_channels`
are ADMISSIBLE. Owner KIND remains a non-criterion, in either direction.
The campaign manifest still refuses any bass owner carrying an invalid
index at authoring time, naming the offending index — not at bench time.
The comparison itself is per-channel: for EACH owner channel `ch`, the
rendered post-limiter peak of that channel's extracted artifact is compared
against `live_peak_all[ch] − recorded_main_volume_db`, under the same
one-sided, bounded, recorded tolerance (R10(c)) applied independently per
channel, the same R4(b) fader-metering-point citation obligation, and the
same pre-fader branch (offset zero) when that citation establishes it.
EVERY owner channel must pass; one failing channel refuses the whole pass.
The prior `devices.playback.channels ≥ 2` mirror-guard retires with the
mechanism that needed it: `_level_pair`'s single-entry fallback mirrored
channel 0's peak into slot 1 whenever CamillaDSP reported only one channel,
and `get_playback_peak_all` has no such fallback — it reports exactly the
channels CamillaDSP returns, so that silent-wrong-channel class is gone
structurally, not by a recorded guard. R3's per-owner-channel rule
(multi-entry `owner_channels`) is consequently REACHABLE rather than
dormant: each owner channel yields its own source observation and its own
candidate, feeding the runner's existing distinct-peak inventory unchanged
— equal peaks across owner channels still collapse to one candidate per
the frozen distinct-measured-candidates and strictly-increasing rule. R3's
minimum-across-owner-channels rule, for any future single-number collapse,
stays in force, unchanged and still conditional: this campaign never
collapses owner channels, so the rule continues to bind only a future
single-number revision — what became live is the per-channel machinery
around it. The full-channel meter accessor this cross-check needed now exists
(#1735); what remains out of scope is only an owner channel index at or
beyond the live pipeline width recorded in `devices.playback.channels` — a
config error the admissibility rule above catches, not a topology class the
cross-check cannot reach.

(b) **Comparison basis:** per owner channel `ch`, the rendered post-limiter
peak of that channel's extracted artifact vs.
`(live_peak_all[ch] − recorded_main_volume_db)`, using R4's recorded fader
value, for the (never-taken, for this build) branch where the fader
application point follows the limiter and the render therefore carries no
fader gain. There is no unindexed live scalar in this rule: every live
value is an element of the `get_playback_peak_all()` reading named in (a).
For the pinned v4.1.3 build, R4(c) resolves PERMANENTLY to "precedes the
limiter," so every render reproduces the recorded fader gain via `--gain`;
combined with the meter always reading AFTER the fader (settled in (a)
above), the offset is always zero and the basis is the rendered peak vs.
`live_peak_all[ch]` unchanged — R4(c)'s two-branch shape, not a refusal, is
retained here as general documented mechanism, not as an open question.
Both peaks, the fader read, and the offset are recorded verbatim, per
channel.

(c) `get_playback_peak_all()` reports the last processed chunk — it reuses
the exact same `c.levels.playback_peak()` websocket call as the
stereo-truncating `get_playback_peak`, so the last-processed-chunk property is
identical — and the live value used for each owner channel is that channel's
max over readings polled at a recorded interval across the role's playback, a
LOWER BOUND on that channel's true peak. The tolerance is therefore
ONE-SIDED: the rendered peak may exceed the offset-corrected live max by up to
the recorded tolerance, and fall below it by no more than the recorded
tolerance. The permissive (exceeds) side is BOUNDED, not merely recorded. What
bounds it is how far a polled maximum can under-read the true maximum of the
signal the meter observes — so the bound is computed from the rendered
post-limiter owner-channel artifact, never from the stimulus: the maximum RISE
in that artifact's level over any window of one recorded poll interval, taken
over the R3 analysis window (stimulus body only). The stimulus is the wrong
basis twice over. It is a different signal from the one at the meter — a role
that drives the limiter into compression, which `sustain_stress` exists to do,
has a post-limiter rise far smaller than its own, so a stimulus-derived bound
is loose in exactly the permissive direction. And R6 mandates that the padded
artifact BEGIN with silence, so a bound taken over it is dominated by the
silence-to-body onset and is effectively unbounded. Using the render's envelope
RATE rather than its level is not circular: a derivation bug that shifts the
whole artifact by a constant — R2(ii)'s −6.02 dB split-mixer hazard is the
canonical case — leaves the rate unchanged, so the bound stays tight and the
offset is caught. That computed bound is recorded alongside the manifest
tolerance, and a manifest tolerance exceeding the computed bound refuses the
campaign. The under-side (falls below) keeps the manifest value. Poll interval,
read count, and tolerance are recorded campaign-manifest inputs, never
defaults.

(d) `get_clipped_samples` is also read at pass start and end; a non-zero
increase across the role's playback refuses the pass — fail-closed evidence
of digital clipping upstream of this check.

(e) Disagreement, an unavailable cross-check observation, or an absent
tolerance refuses the pass.

The cross-check binds the POST-limiter render. The pre-limiter render —
whose peak becomes `limiter_threshold_dbfs` — is bound through the shared
derivation function and the exact `names[:i]`/`names[:i+1]` boundary defined
in R2; the executor PR pins that boundary with a hardware-free test that a
pre-limiter config retaining `names[:i-1]` refuses.

This is still the only rule that binds a render to reality; it is not
optional.

## Post-hoc realization of the frozen "tap discontinuity" abort

The frozen abort condition "tap discontinuity" is realized post-hoc: the
rendered artifact's frame count must equal the stimulus artifact's frame
count exactly, and its SHA must be recorded before analysis. A mismatch is a
`refused` pass with the partial artifacts preserved — it can no longer abort
active playback, and no other frozen abort condition changes.

## Consequence for the merged runner (explicitly authorized)

Under R9, sweep and sustain artifacts do not exist until the activation
window exits, so `RoleExecutor.run_discovery` — for its sweep and sustain
artifacts only — can no longer return them synchronously.
`RoleExecutor.run_candidate` KEEPS returning `digital_transfer_probe`
unchanged and synchronously (R9's frozen step 4, inside the window and the
live activation, at the safe floor); only its `sweep_core` and
`sustain_stress` PCM/peak fields move to after the window closes. The
runner (`jasper/bass_extension/bench/runner.py`) already assembles
`bundle.build_sweep_record` and calls `_classify_candidate` outside
`deps.open_window()`'s block, so the executor-binding PR's change is
confined to where the `sweep_core` and `sustain_stress` PCM/peak values are
produced; no other reused module changes, and the runner's
activation/restoration/disposition ownership is untouched.

## Receipts are bundle files, not schema fields

Every receipt this amendment mandates — the derivation receipt, the
determinism receipts, R10's recorded cross-check evidence, and the retained
identity artifact — is a FILE in the bundle directory, not a field added to
any closed schema object. No key is added to `measured_context`, a source
observation, a candidate, or the sweep/sustain/transfer records: those are
exhaustive "has exactly" lists in `limiter-evidence-protocol.md`, and an
unknown field makes the producer refuse the bundle as `inconsistent`. These
receipts are review evidence for a human or the executor-PR's own tests, not
producer inputs. R10's poll interval, read count, and tolerance — including the
tolerance's computed bound — ride the campaign manifest instead, whose field
list the frozen protocol does not close. Where values ride the campaign
manifest, the executor PR extends `manifest.py`'s request-field set to carry
them — its `to_dict()` drops unknown keys today.

## What this amends in the frozen contracts — explicitly

Two realization sentences: `limiter-evidence-protocol.md`'s "Both taps must
be non-mutating and content-addressed," and `limiter-bench-runner-protocol.md`'s
"non-mutating, content-addressed reads of the exact sample stream at the
limiter input and immediately after it" — both realized as the deterministic
offline re-render defined above (taps remain non-mutating per R9's argv
contract, and content-addressed per R1–R6). Plus the ordering clarification
in R9 and the runner-sequencing authorization above. Also: the frozen "tap
discontinuity" abort condition, which presupposed a live tap, becomes the
post-hoc frame-count check defined above — a `refused` pass with partial
artifacts preserved, not an immediate playback abort. No other frozen abort
condition changes. Everything else — roles, passes, refusal taxonomy, bundle
schema, producer contract, `detector_reference` — is untouched.

`tap_implementation_id` remains a non-empty trimmed **string**, as the frozen
schema requires. Its value is the canonical lowercase SHA-256
`json_fingerprint` over the object `{camilladsp_version, binary_sha256,
derivation_function_version}` — `binary_path` is excluded from the
fingerprint (a filesystem path is not a stable identity input) and lives only
in the retained identity artifact alongside the fingerprinted fields, so the
full resolution stays replayable and inspectable. That identity artifact is
retained as a bundle artifact. The bundle schema is unchanged. A future
trusted production caller must reproduce this exact string in
`required_context`, or the producer returns `stale`. `camilladsp_build_id` is
composed as `camilladsp-<version>-<binary_sha256[:12]>`; the recorded
`camilladsp_version` and `binary_sha256` must reproduce it exactly, or the
campaign refuses.

## What this does NOT authorize

No production wiring of `produce_limiter_thresholds`; no profile writer; no
scheduler; no live per-stage tapping attempts; no CamillaDSP fork or patch;
no new daemon, route, socket, timer, or unit. The executor-binding PR is
authorized only **after** this amendment is accepted, and that PR still faces
its own independent adversarial safety review (gate 2 of
`limiter-bench-runner-protocol.md`) before any hardware playback. Jasper's
supervised bench session remains the only path to an accepted bundle.

## Tests (land with the executor-binding PR, hardware-free)

- Derivation over the raw mapping: three-part prefix proof (devices-block
  allowlist diff; `filters`+`mixers` identical after re-serialization,
  including a mixer source's `channel`/`gain`/`inverted` — an explicit case
  for the −6.02 dB split mixer; derived `pipeline` equals live `pipeline`
  truncated at the owner step, key-for-key); the exact `names[:i]` /
  `names[:i+1]` boundary math (post-limiter retains `names[:i+1]`,
  pre-limiter retains `names[:i]`, a config retaining `names[:i-1]`
  refuses); the pre-limiter re-proof predicate applied to the retained
  prefix (`bass_ext_lt` before `bass_ext_subsonic`, owner channels exact,
  limiter name absent) as a separate hardware-free test from
  `_prove_active_graph`'s post-limiter proof; the R7 owner-path computation
  (walking retained mixers backward from the owner channel); channel-width
  preservation + recorded owner index; `devices.capture` key handling
  (`type`/`filename` swapped, `device`/`format`/`channels` keys DELETED —
  S-6: `CaptureDeviceWavFile` has no `channels` field at all, settled by
  Revision 7's errata; the live `devices.capture.channels` value survives
  only as a validation input) plus artifact-header-vs-`devices.samplerate`/
  live-`devices.capture.channels` match; `devices.playback` key handling
  (`type`/`filename` swapped, `device` deleted, `format` set to the
  receipted precision, `channels` at live pipeline width); fader
  reproduction per the receipted application point, exercised as a pure
  unit test against the derivation function for BOTH receipt placements
  (the receipt is a direct input; S-7 — for the pinned v4.1.3 build, only
  the "precedes the limiter" placement is reachable, settled by Revision
  7's errata: the process-wide main fader always runs before every
  pipeline step, so R4(c) resolves PERMANENTLY to that branch. The
  "follows the limiter" placement is exercised as the general, DEAD CODE
  branch for this CamillaDSP version — never the other way around).
- Refusals: non-allowlisted stage on the owner path, a filter type outside
  the six-member allowlist, a step carrying `bypassed`, Async resampler
  block, fader drift between the bracketing reads (`refused` arm), mute,
  version ≠ v4.1.3, set-but-different `JASPER_CAMILLADSP_BIN`, argv carrying
  `--statefile`/port, each of R6a's four elements individually unproved,
  insufficient lead-in and lead-out including the Conv-impulse-length case.
- R6a ingress transparency: isolation (mux STATUS `test_source`/
  `active_source`/`test_owner`), lane state (`muted`, `resampler.armed`,
  `trim.pending`, `xrun_count`/`catchup_resync_frames`/`catchup_events`
  unchanged), no foreign audio at the mix (fan-in STATUS
  `tts.program_duck_active`/`tts.pending_frames` at start and end,
  `tts.flushed_frames`/`tts.dropped_audio_frames` unchanged), and
  bit-width/geometry (16-bit at the lane's rate/channels) each tested
  independently, proved and individually unproved.
- Determinism-receipt logic per config shape (mocked renders, hash
  compare), including the per-candidate pre-limiter shape rule (a distinct
  shape per candidate because `clip_limit` rides the retained `filters`
  block verbatim; receipted per candidate; never reused across candidates).
- R10 cross-check logic: `get_playback_peak_all()` returning `[]`, or a
  list shorter than `max(owner_channels)+1`, → `cross_check: unavailable`
  and refuses; the manifest tolerance's computed bound (the rendered
  post-limiter owner-channel artifact's maximum per-poll-interval level
  RISE over the R3 analysis window) — a manifest tolerance exceeding it
  refuses the campaign; plus a synthetic compressed-render case where a
  stimulus-derived bound would have admitted a rendered peak that the
  render-derived bound refuses, and a case proving the lead-in silence does not
  enter the bound; one-sided tolerance (over-by-more-than-tolerance refuses,
  under-by-more-than-tolerance refuses, agreement within tolerance passes); a
  `get_clipped_samples` increase refuses; disagreement or an absent tolerance
  refuses (mocked live peak, mocked fader read); a per-channel comparison case
  on a 4-channel map with `owner_channels` `[0, 2]` where one channel's
  comparison passes and the other's fails — the single failing channel refuses
  the whole pass; campaign-manifest authoring refusal, before any bench pass
  runs, on any owner index ≥ `devices.playback.channels` (an invalid index,
  named); the R3 minimum-across-owner-channels invariant for any future
  single-number collapse remains a pure unit test of the collapse helper,
  now exercisable with the realistic multi-channel peaks the 4-channel case
  above produces.
- Post-hoc tap-discontinuity: frame-count mismatch → `refused` with partials.
- R3 analysis window: lead-in/lead-out frames excluded from the peak
  search; a multi-entry `owner_channels` yields one observation and one
  candidate per channel, never collapsed before entering the runner's
  distinct-peak inventory; equal peaks across owner channels collapse
  inside that existing inventory, unchanged by this amendment.
- Round-trip: synthetic rendered artifacts → bundle emitter →
  `produce_limiter_thresholds` acceptance, including the string
  `tap_implementation_id` fingerprint (over `{camilladsp_version,
  binary_sha256, derivation_function_version}`, no `binary_path`) and its
  retained identity artifact.
- The on-bench validation of real renders is Jasper's supervised campaign —
  deliberately not an automated test.

## Registration

On acceptance this document is added to the amendment list in
[`docs/bass-extension-waves/README.md`](README.md) alongside
`limiter-bench-runner-protocol.md` and `limiter-bench-runner-activation.md`
(the latter is currently missing from that list — fix both in the acceptance
PR), and the plan's Wave-status table — which today has rows for Waves 0–7
only — gains a new tap-realization row recording that the tap realization
is settled; the acceptance PR CREATES that row rather than updating one.
Status is stated in the plan table only — the acceptance PR also replaces
this document's draft status banner with a status-free accepted note, so
the plan table becomes the sole status surface.

## Review gates

1. This amendment: independent adversarial review to 0/0 + maintainer
   acceptance → merges beside the runner protocol docs.
2. Executor-binding implementation PR: its own adversarial review to 0/0.
3. Jasper's supervised bench campaign produces the bundle; the producer and a
   later Wave-4 revision take it from there, unchanged by this document.

## Changelog

- **Rev 7 (errata) (2026-07-25):** addresses the executor-binding PR's
  gate-2 review, which independently confirmed (including the
  `--gain`/`initial_volumes[0]`/no-startup-ramp mechanism, `src/bin.rs`) the
  citations this document had marked as RECORDED OBLIGATIONS pending
  verification. No rule text changes beyond recording these now-settled
  facts and their citations, and correcting spellings/behavior this
  amendment stated incorrectly before the pinned v4.1.3 source was checked
  line-for-line:
  - **R3, `Wav` → `WavFile`, throughout.** The real config-schema capture
    type name is `WavFile` (`src/config/mod.rs`: `enum CaptureDevice { …
    WavFile(CaptureDeviceWavFile), … }`, no `#[serde(alias=...)]`) — "Wav"
    was never a real type name. `CaptureDeviceWavFile { filename,
    extra_samples?, labels? }` is `#[serde(deny_unknown_fields)]` and
    declares **no `channels` field at all** — `devices.capture.channels` is
    therefore DELETED from the derived capture block (like `device`/
    `format`), not "unchanged" as this document previously stated; the live
    value becomes a pure validation input compared against the artifact
    header before any render starts, and is recorded in the derivation
    receipt. `PlaybackDevice::File` (the playback side) *does* declare a
    required `channels: usize` field, so `devices.playback.channels`
    staying at the live pipeline width is unaffected.
  - **R4(b)/(c), the fader application point — now a settled fact, not a
    recorded obligation pending citation.** `src/pipeline.rs`
    `Pipeline::process_chunk`: `self.volume.process_chunk(&mut chunk)` (the
    process-wide main-fader stage, built in `Pipeline::from_config` from
    `processing_params.current_volume(0)`) runs BEFORE the `for step in
    &mut self.steps` loop — i.e. before every configured pipeline step,
    unconditionally. The main fader therefore ALWAYS precedes the owner
    limiter for this build: R4(c) resolves PERMANENTLY to "reproduce the
    recorded fader gain" (the "follows the limiter, apply no fader gain"
    branch is dead code for this CamillaDSP version, not merely
    "currently unreachable on every JTS-emitted graph" as this document
    previously stated — that prose described a different, PIPELINE-embedded
    `Volume`-type FILTER step, which R7 separately refuses on the owner
    path; it is not this always-present process-wide fader). R4(c)'s
    now-false "currently unreachable… exists so a future graph change fails
    loudly" sentence is deleted. Every render reproduces the fader gain via
    `--gain <fader_db>` (`src/bin.rs`: `Arg::new("gain")` sets
    `initial_volumes[0]` — the same index `Pipeline::from_config` reads for
    `self.volume` — and `filters/basicfilters.rs`'s `Volume::new` starts
    with `ramp_step: 0`, so the first processed chunk already applies it,
    no startup ramp). `rust/…/alsa_backend/device.rs`'s playback loop runs
    `chunk.update_stats(&mut chunk_stats)` on the chunk received from the
    processing thread — i.e. AFTER the full `pipeline.process_chunk` (fader
    plus every step) already ran — so `get_playback_peak_all()` always
    reads downstream of the fader too. Combined, R10(b)'s offset is always
    0 dB for this build (never the written `−recorded_main_volume_db`
    term, which is retained as the general, never-taken-for-this-build
    branch so a reviewer can audit the reasoning without trusting a
    hardcoded constant). R10(a)'s "VOID until this citation is recorded"
    marker is retired — this is the amendment's most safety-load-bearing
    finding, and it is settled.
  - **R3, processing precision — recorded as `F64_LE`.** The deployed
    binary (`deploy/install.sh`'s `camilladsp-linux-aarch64.tar.gz` for
    `CAMILLA_VERSION=v4.1.3`) is built by upstream's `linux_aarch64` /
    "Build with Alsa only" release job as plain `cargo build --release` —
    no `--features` flag, so no `32bit` Cargo feature. `src/lib.rs`
    (`#[cfg(feature = "32bit")] pub type PrcFmt = f32; #[cfg(not(feature =
    "32bit"))] pub type PrcFmt = f64;`) then pins `PrcFmt = f64` for this
    exact build. The device-format spelling for that precision, in this
    pinned build's `FileSampleFormat`/`BinarySampleFormat` enums
    (`src/config/mod.rs`, both `{…, S24_4_RJ_LE, S24_4_LJ_LE, S24_3_LE,
    S32_LE, F32_LE, F64_LE}`, `deny_unknown_fields`), is **`F64_LE`** —
    "FLOAT64LE" is a retired v1/v2 spelling this build rejects outright.
    Every "FLOAT64LE" reference in this document's live rule text (R3) is
    corrected to `F64_LE`.
  - **R3, `queuelimit`/`target_level` — recorded resolved-unchanged.** Both
    are declared optional on `Devices` (`src/config/mod.rs`, defaults 4 and
    `chunksize` respectively), and the README documents no file-device
    -specific requirement — the guidance ("only changed if the capture
    device can provide data faster than playback can consume it, like the
    ALSA 'cdsp' plugin") is about real-time device pacing, meaningless for
    a batch `WavFile`/`File` pass with no live clock to steer. Derivation
    therefore leaves both keys untouched at their live values — corrected
    from this document's previous "→ the values CamillaDSP requires for
    file↔file operation," which asserted a transformation that does not
    exist — and records them in the derivation receipt (live and derived,
    identical) precisely because they are unchanged, not despite it.

  This entry uses "addresses," never "incorporates" or "resolves," matching
  prior revisions' own convention (Rev 2 is the one exception — it uses
  "incorporated"). The plan's `4-taps` wave-status row's accepted
  parenthetical is annotated "revision 7 (errata)" rather
  than rewritten — no rule text changes beyond recording these facts and
  citations.
- **Rev 6 (2026-07-24):** addresses maintainer direction to dissolve the
  mono-only scope, step 2 of
  [#1723](https://github.com/jaspercurry/JTS/issues/1723), effective now
  that the full-channel meter accessor it depended on has merged:
  `CamillaController.get_playback_peak_all`
  ([#1735](https://github.com/jaspercurry/JTS/issues/1735)) returns every
  playback channel's peak in channel order, with no truncation and no
  mirroring. R10(a) is rewritten: the cross-check reads
  `get_playback_peak_all()` instead of the stereo-truncating
  `get_playback_peak`/`_level_pair`; admissibility becomes a per-entry
  index check — every entry of `owner_channels` must satisfy `0 ≤ index <
  devices.playback.channels` — replacing the old two-condition
  exactly-one-entry-at-0-or-1 topology restriction, so multi-entry
  `owner_channels` are now ADMISSIBLE and owner KIND remains a
  non-criterion. The comparison itself is per-channel, with unchanged
  per-channel rigor: EVERY owner channel gets its own
  `live_peak_all[ch] − recorded_main_volume_db` comparison under the SAME
  one-sided, bounded, recorded tolerance, the same R4(b)
  fader-metering-point citation obligation, and the same pre-fader branch,
  and any single failing channel refuses the whole pass — this dissolves
  scope, it does not loosen the check. `get_playback_peak_all()` returning
  `[]`, or a list shorter than `max(owner_channels)+1`, is `cross_check:
  unavailable` and refuses, replacing the old owner-index-outside-{0,1}
  trigger. The `devices.playback.channels ≥ 2` mirror-guard retires along
  with the mechanism it existed to guard — `_level_pair`'s single-entry
  fallback, which mirrored channel 0's peak into slot 1; `get_playback_peak_all`
  has no such fallback, so that silent-wrong-channel class is gone
  structurally. R3's per-owner-channel rule (multi-entry `owner_channels`),
  UNREACHABLE and forward-compatible-only since Rev 5, is now REACHABLE
  and live; its minimum-across-owner-channels rule for a future
  single-number collapse stays in force, unchanged and still conditional —
  what became live is the per-channel machinery around it. The Tests section's R10 bullet is updated to
  match: refusal on any owner index ≥ `devices.playback.channels`, refusal
  on an unavailable/short accessor reading, and a new per-channel
  comparison case on a 4-channel map with `owner_channels` `[0, 2]` where
  one channel passes and the other fails. The plan's `4-taps` wave-status
  row Scope sentence is updated to match; its acceptance date and
  revision-count claims are updated in this same change to reflect this
  revision's own independent adversarial review — the same gate every
  prior revision passed through. This entry uses "addresses," never "incorporates" or
  "resolves," matching Rev 3 through Rev 5's own convention.
- **Rev 5 (2026-07-24):** addresses the fourth 2026-07-24 adversarial review of
  Rev 4 (2 Blockers, 8 Nits) — the independent review that follows this
  revision decides whether each is actually resolved. R10(a)'s scope is
  corrected from an owner-KIND test ("the mono woofer-way owner on emitted
  maps") to two explicit conditions on the recorded `owner_channels` — (1)
  exactly one entry, (2) that entry is 0 or 1 — checked independently of owner
  kind: a `woofer_way` owner at index 2 (e.g. a mono 3-way) now refuses, and a
  `local_sub` owner satisfying both conditions (the mono 1-way + local-sub
  shape, where `SUPPORTED_LAYOUTS`/`DRIVER_ROLES_BY_WAY[1]` force
  `main_output_count == 1` and `local_subwoofer.physical_output_index == 1`) is
  now explicitly admitted rather than swept into the old blanket local_sub
  exclusion. A new `devices.playback.channels >= 2` requirement applies when
  the owner index is 1, so `_level_pair`'s single-entry-input fallback (which
  mirrors channel 0 into slot 1) can never alias channel 0's peak onto channel
  1's. R3's multi-entry-owner-channel rule is now marked UNREACHABLE under
  condition (1) and retained only as a forward-compatible invariant with its
  own pure unit test, mirroring R4(c)'s pre-limiter fader-branch treatment; the
  Tests bullet for the authoring-time refusal gained the matching per-condition
  breakdown plus the complementary admission case. R10(c)'s permissive-side
  bound is now computed from the RENDERED post-limiter owner-channel artifact's
  own maximum per-poll-interval level RISE over the R3 analysis window, never
  from the stimulus: the stimulus is a different signal from the one at the
  meter (`sustain_stress`'s compression makes a stimulus-derived bound loose in
  exactly the permissive direction), and R6's mandated lead-in silence would
  make a stimulus-envelope bound effectively unbounded. Using the render's
  envelope RATE rather than its level keeps the bound non-circular — a
  constant-offset derivation bug (R2(ii)'s −6.02 dB split-mixer hazard) leaves
  the rate unchanged, so the bound stays tight and the bug is still caught; the
  Tests bullet gained a synthetic compressed-render case (proving a
  stimulus-derived bound would have wrongly admitted a rendered peak the
  render-derived bound refuses) and a case proving the lead-in silence does not
  enter the bound. R4(d) and R5's "the protocol's pinned v4.1.3" is now cited
  concretely: v4.1.3 is the version `deploy/install.sh`'s `CAMILLA_VERSION`
  installs, and the build whose `filters/limiter.rs`
  `limiter-evidence-protocol.md` cites for the `soft_clip` transfer. R2's
  pre-limiter re-proof is now explicitly a DISTINCT prefix predicate over the
  retained prefix, not "the same predicate" applied elsewhere. R2(i)'s
  devices-block diff clause now names both the capture/playback device swap and
  R3's closed normalization allowlist as the only permitted differences, "and
  by nothing else." R6a's preamble now attributes only elements (ii)-(iii) to a
  fan-in STATUS read; element (iv) is attributed to the artifact header checked
  against fan-in STATUS's `sample_rate` field plus the compile-time
  `CHANNELS`/`FORMAT` constants (`mixer.rs`; element (iv)'s own body already
  cited `FORMAT` — a `CHANNELS` citation there is a candidate follow-up, not
  made in this revision). R10(b) gained the pre-fader branch explicitly: when
  the recorded citation shows the meter reads before the main fader, the offset
  is zero and the basis is the rendered peak vs. `live_peak_dbfs` unchanged
  (R4(c)'s two-branch shape), not a refusal. The intro's provenance-split
  sentence now assigns correctly — the second bullet (CamillaDSP's
  one-playback-device-per-process claim) and the second clause of the first
  bullet (the CamillaDSP websocket-API claim) are installed-build properties
  needing citation; the remainder, including the first bullet's JTS-wrapper
  clause, is tree-verified. The "Receipts are bundle files" section now states
  that a value riding the campaign manifest requires the executor PR to extend
  `manifest.py`'s request-field set to carry it, since its `to_dict()` drops
  unknown keys today. R6 gained a lead-out rule: at least the owner path's
  total group delay (summed `Delay` stages plus the longest `Conv` impulse
  response), also recorded. Registration now states explicitly that the plan's
  Wave-status table has rows for Waves 0–7 only today, so the acceptance PR
  CREATES the tap-realization row rather than updating an existing one,
  alongside the already-noted `limiter-bench-runner-activation.md` absence from
  the waves README's amendment list — both fixed in the acceptance PR. Status
  banner and this entry use "addresses," never "incorporates" or "resolves,"
  matching Rev 3 and Rev 4's convention.
- **Rev 4 (2026-07-24):** addresses the third 2026-07-24 adversarial review
  of Rev 3 (8 Blockers, 4 Should-fixes, 3 Nits) — the independent review
  that follows this revision decides whether each is actually resolved.
  R3's capture/playback device-swap contract corrected: the capture swap
  sets `type`/`filename` and DELETES the live `device`/`format` keys
  (`Wav` takes its geometry from the artifact header, not config);
  `devices.capture.channels` is unchanged; the artifact header's rate,
  channel count, and (16-bit, per R6a(iv)) sample format must match
  `devices.samplerate`/`devices.capture.channels`. The playback swap
  deletes `device` and sets `format` to the receipted precision —
  `devices.playback.format` is now in R3's normalization allowlist. R2
  corrected: `_prove_active_graph` (`jasper/bass_extension/bench/
  activation.py`) applies only to the post-limiter config — it
  structurally requires the limiter name present and ordered, so it cannot
  validate a pre-limiter truncation; the pre-limiter config is re-proved by
  a distinct prefix predicate (its own module; `activation.py` untouched)
  requiring `bass_ext_lt`/`bass_ext_subsonic` on the owner channels with
  the limiter name absent. R2 also gained the exact boundary math: `i` is
  the limiter's index in the live owner step's `names`; post-limiter
  retains `names[:i+1]`, pre-limiter retains `names[:i]`; any other length
  refuses, both lengths and the full `names` list are recorded. R6a(iii)
  renamed "No foreign audio at the mix" and rewritten onto fan-in STATUS
  counters (`tts.program_duck_active`/`tts.pending_frames` at start and
  end, `tts.flushed_frames`/`tts.dropped_audio_frames` unchanged) because
  `program_duck_active` alone misses a standalone cue's `cue_duck_gain`
  duck (`rust/jasper-fanin/src/tts.rs`); the `MEASURE_PAUSE` voice lease is
  named as the mechanism but dropped as an assertable element
  (`measurement_window()` yields no handle and only warns on failure),
  while mux-gate isolation (element (i)) is kept as lease-backed and
  observable. R10's tolerance is now BOUNDED, not merely recorded: the
  permissive side may not exceed a bound computed from the padded stimulus
  artifact's own level envelope, and a manifest tolerance exceeding that
  bound refuses the campaign. R10(a) gained an explicit scope limit — the
  cross-check is realizable only for a mono woofer-way owner (channel 0 or
  1); stereo owners and any local_sub owner are out of this amendment's
  scope and must be refused at manifest-authoring time, not bench time.
  R10(a)/(b)'s pre/post-fader metering claim is now a recorded obligation
  on the R4(b) pattern, and the `−recorded_main_volume_db` offset is void
  until that citation lands. R10 also binds the pre-limiter render (whose
  peak becomes `limiter_threshold_dbfs`) through the same
  `names[:i]`/`names[:i+1]` boundary, pinned by a refusal test on
  `names[:i-1]`. Two upstream-fact bullets in the opening "not realizable"
  list are now flagged as pinned-v4.1.3-build properties recorded with
  source citations at executor-PR time (the other two are already verified
  against this tree), and R3 gained the same recorded-obligation treatment
  for the `Wav` type's existence/header semantics and the
  `queuelimit`/`target_level` values. R8's pre-limiter shape-reuse rule is
  REVERSED: the pre-limiter render is `clip_limit`-independent only in the
  audio domain — R2(ii) retains the live `filters` block verbatim, so the
  candidate's `clip_limit` still rides the byte content — so every
  candidate gets a distinct shape, receipted per candidate, never reused.
  A new "Receipts are bundle files, not schema fields" section states that
  every receipt this amendment mandates is a bundle-directory file, never a
  key on `measured_context` / a source observation / a candidate / a
  sweep-sustain-transfer record (closed "has exactly" lists that refuse
  unknown fields), and that R10's poll interval/read count/tolerance ride
  the campaign manifest, whose field list the frozen protocol leaves open.
  R3's per-owner-channel paragraph now says explicitly that per-channel
  observations enter the runner's existing distinct-peak inventory
  unchanged (`runner.py`'s `distinct.setdefault`) and equal peaks still
  collapse there — this amendment only forbids collapsing owner channels
  BEFORE that inventory. The "Consequence for the merged runner" section is
  corrected: only `run_discovery`'s sweep/sustain artifacts and
  `run_candidate`'s `sweep_core`/`sustain_stress` PCM/peak fields move to
  after the window closes; `run_candidate` keeps returning
  `digital_transfer_probe` synchronously at its frozen step-4 position, and
  the runner (`runner.py`) already assembles `build_sweep_record` and
  classifies outside the window, so the executor-binding PR's change is
  confined to where the sweep/sustain PCM/peaks are produced. R9's render
  bounds (`RLIMIT_AS`/`RLIMIT_CPU`, the nice value, the timeout) are now
  recorded campaign-manifest inputs rather than unstated constants, and the
  free-space guard is scoped to renders outstanding for the whole campaign,
  not just the current target. The R6a preamble now correctly attributes
  element (i) to a mux STATUS read and only (ii)-(iv) to fan-in STATUS,
  matching what element (i) itself already said; R6a(iv)'s S16 citation
  moves from `config.rs` (a USB-DIRECT-lane-only comment) to `mixer.rs`'s
  module docstring and `FORMAT` const, the actual general-mixing citation.
  Registration gained one sentence: the acceptance PR deletes the status
  banner, leaving the plan table as the sole status surface. Status banner
  and this entry use "addresses," never "incorporates" or "resolves,"
  matching Rev 3's own convention.
- **Rev 3 (2026-07-24):** addresses the second 2026-07-24 adversarial review
  of Rev 2 (5 Blockers, 13 Should-fixes, 3 Nits) — the independent review
  that follows this revision decides whether each is actually resolved. R6a
  replaced entirely: isolation is mux's selected-input gate
  (`_measurement_gate_held` on `test_source`/`active_source`/`test_owner`),
  not per-lane mutes; four proof elements (isolation; lane state incl.
  `resampler.armed`/`trim.pending`/xrun-and-catchup counters; no-duck via
  `program_duck_active` plus the held `MEASURE_PAUSE` lease; S16
  bit-width/geometry); no assertable per-lane gain (fan-in exposes none).
  R10 replaced entirely: cross-check gated on the owner-channel index being
  0 or 1 (`_level_pair` returns only those two), else `unavailable` and
  refuse; a fader-corrected comparison basis; a one-sided tolerance around
  `get_playback_peak`'s lower-bound polling; a `get_clipped_samples`
  non-zero-increase refusal (N-3). R2's derivation moved onto the raw
  mapping — `yaml.safe_load` of `active_config_raw`, never a `GraphView` —
  because `view_from_camilla_dict` is a lossy projection (drops non-`Filter`
  steps, `bypassed`, `mixers`, `devices`) that would have silently hidden
  the −6.02 dB `MONO_SUM_GAIN_DB` split mixer; `view_from_camilla_dict` is
  now used only to re-prove owner-step invariants via
  `activation.py::_prove_active_graph`, not `_assert_bass_extension_safe`
  (which hard-codes `view_from_emitted_text` and the baseline `clip_limit`);
  the prefix proof is now three-part over the raw mapping, naming the
  split-mixer hazard explicitly. R7 gained a formal "owner path" definition.
  R9's ordering language rewritten to remove the contradiction between "runs
  outside the window" and the frozen `digital_transfer_probe` step-4
  position (that step-4 position is unchanged — it was already the sole
  exception in Rev 2, just confusingly stated); its bounds section rewritten
  from a reference to `scripts/pi-run-diagnostic.sh` (a laptop-side SSH +
  `systemd-run` wrapper the runner protocol's no-new-unit rule forbids, and
  whose 10-minute ceiling would kill a campaign mid-activation) to
  process-local `RLIMIT_AS`/`RLIMIT_CPU`/`os.nice()` on top of
  `dsp_apply.py`'s `--check` subprocess shape; its byte-budget sentence
  rewritten to a per-render estimate checked before every render. R3 gained
  an analysis-window rule (stimulus-body-only peaks, excluding R6's lead-in/
  lead-out) and per-owner-channel candidates (never collapsed when
  `owner_channels` has more than one entry), a `devices.capture`-key
  enumeration matching R6a(iv), and a corrected precision sentence (recorded
  precision, not unconditional FLOAT64LE). R4(a) reworded to the `refused`
  arm (not the producer's `INCONSISTENT`); R4(c) documents the pre-limiter
  fader branch as a currently-unreachable tripwire, and its Tests bullet now
  says both placements are pure unit tests against the derivation function.
  R8's "shape" is formally defined (exact byte content, keyed by SHA-256, no
  per-target floor) plus the pre-limiter shape-reuse rule (N-2).
  `stimulus_generator_identity` is no longer redefined — a new
  `padded_stimulus_assembly_identity` field carries the padded-assembly
  identity instead. R6's lead-in rule extended for `Conv` impulse-response
  length (Conv itself stays in the R7 allowlist). `tap_implementation_id`'s
  fingerprint object drops `binary_path` (kept only in the retained identity
  artifact); `camilladsp_build_id`'s composition is now pinned as
  `camilladsp-<version>-<binary_sha256[:12]>`. The post-hoc
  tap-discontinuity realization is now also cross-referenced from "What this
  amends." Status banner and this entry use "addresses," never "incorporates"
  or "resolves," per the review's own instruction not to claim incorporation
  as settled fact.
- **Rev 2 (2026-07-24):** incorporated the full 2026-07-24 adversarial
  review (6 Blockers, 13 Should-fixes, 5 Nits): owner-step `names`-list
  truncation model (was: pipeline-step cut); closed device-key normalization
  allowlist incl. `enable_rate_adjust → false` (was: "no other key
  changes"); `Wav` capture + owner-channel index + precision receipt;
  receipted fader application point with pre/post-limiter placement rule and
  v4.1.3 pin; binary resolution from the running unit with env-override
  refusal; runner-owned padding assembly; new R6a ingress-transparency
  proof; stage-type allowlist (was: denylist); per-shape determinism
  receipts; render argv isolation + bounded-runner + byte-budget rules;
  transfer-probe ordering preserved; new R10 live cross-check; post-hoc
  tap-discontinuity realization; explicit RoleExecutor/runner re-sequencing
  authorization; string `tap_implementation_id` fingerprint; registration
  fixes.
- **Rev 1 (2026-07-24):** initial draft.
