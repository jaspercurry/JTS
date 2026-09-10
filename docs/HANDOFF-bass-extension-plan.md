# Speaker, Room, and bass extension: implementation plan

Updated 2026-09-10 against `origin/main` at `d397cabe2`. This replaces the
previous checklist in the existing exempt plan (ADR-0229). Recheck code and PR
state before acting; this is a work plan, not a second store of runtime state.

## Outcome and scope

Make three clear LLM-led programs from one tuning toolbox:

1. **Speaker:** linearize drivers and align their crossover where speaker
   measurements are reliable.
2. **Room:** move the speakers to their listening locations, measure around
   the listener, and fit the accepted speakers to that area.
3. **Bass extension:** start from the accepted speaker and Room response, add
   useful low bass at low/moderate levels, and reduce only the extra extension
   smoothly as volume or bass demand rises.

The user gets a short explanation, placement instructions, useful measurements,
a measured candidate, and an explicit save action. A later session must find
earlier evidence and continue from it. The LLM chooses experiments and judges
results; code owns arithmetic, playback, capture, identity, and saving.
Defaults guide the process without forcing a campaign.

Finish the existing implementation. Reuse or retire code in scope. Do not add
a second recorder, candidate bank, profile file, DSP writer, volume controller,
or general workflow engine. Heavy analysis stays on the laptop. Sealed,
ported, and passive-radiator cabinets share the empirical workflow; detailed
enclosure models and mandatory nearfield measurements are not prerequisites.
Cardioid, maximum-output certification, thermal endurance, and a new audio
processor remain outside this release.

## Research that guides the design

- [Balazs Bank, AES 134 paper 8826 (2013), author PDF](https://home.mit.bme.hu/~bank/publist/aes134.pdf)
  and [AES record](https://aes2.org/publications/elibrary-page/?id=16727).
  Local copy: `/Users/jaspercurry/Downloads/bank_aes134.pdf`. Sections 2 and 4.3
  guide speaker-then-room correction and multiple positions. Correct direct
  sound where gating is useful, then assess the complete pre-equalized in-room
  response. Exact speaker/room separation at low frequencies is unnecessary.
  The example crossover and target cutoff are not universal JTS settings.
  Multiple positions are a proposed extension, not proof of an optimal eleven
  positions. Flat full-range room response differs from flat direct sound;
  make the target explicit.
- [Dutch & Dutch RoomMatching](https://dutchdutch.com/pages/roommatching)
  and [guided app workflow](https://dutchdutch.com/pages/app).
  Placement, known speaker response, and guided listening-area measurements
  form one process. Adopt that workflow without assuming their hardware models,
  acoustic design, or proprietary algorithms apply to arbitrary JTS cabinets.
  Eleven positions is our configurable default, not a required count.
- [Microsoft US12342139B2](https://patents.google.com/patent/US12342139B2/en)
  ([readable patent text](https://patents.justia.com/patent/12342139)):
  volume-dependent Linkwitz transform and multiband compression. Borrow fitted
  response shaping and selective reduction of excess boost. Our parallel
  added-bass branch is a simpler implementation choice, not an exact copy of
  that patent. Do not force a sealed-box model onto ported or radiator systems.
- [Google US10200003B1](https://patents.google.com/patent/US10200003B1/en)
  ([readable family text](https://patents.google.com/patent/CN110679157B/en)):
  adjust extension with headroom and signal demand. This program seeks more
  real fundamental output, not the optional synthetic-harmonic/virtual-bass mode.
- [Klippel amplitude compression](https://www.klippel.de/fileadmin/klippel/Files/Know_How/Application_Notes/AN_12_Amplitude_Compression.pdf),
  [tone-burst measurement](https://www.klippel.de/manuals/frequencyresponse-distortion/tbm/tbm.html),
  and [room effects on distortion](https://klippel.de/manuals/frequencyresponse-distortion/isc/isc.html).
  Compare output with input across levels, qualify noise, and retain harmonics
  by frequency. Received distortion is not isolated driver distortion, and
  microphone SPL is not a cone-excursion measurement.

## Starting code and work in flight

Navigation pointers; inspect current callers before editing:

| Concern | Existing owner |
| --- | --- |
| Rules and optional workflow | [doctrine](measurement-loop-doctrine.md), [runbook](tuning-operator-runbook.md), [methodology](tuning-methodology.md) |
| Positions and defaults | [measurement_plans.json](../jasper/active_speaker/measurement_plans.json), [measurement_programs.py](../jasper/active_speaker/measurement_programs.py) |
| Capture | [cli/measure.py](../jasper/cli/measure.py), [program.py](../jasper/audio_measurement/program.py), [wired_capture.py](../jasper/audio_measurement/wired_capture.py) |
| Layer composition | [measurement_emit.py](../jasper/active_speaker/measurement_emit.py) |
| Evidence discovery | [inventory.py](../jasper/cli/round_views/inventory.py), [record_index.py](../jasper/active_speaker/crossover_v2/record_index.py), [evidence_packet.py](../jasper/active_speaker/crossover_v2/evidence_packet.py) |
| Shared identity and analysis | [evidence_identity.py](../jasper/audio_measurement/evidence_identity.py), [fingerprinted_record.py](../jasper/audio_measurement/fingerprinted_record.py), [frequency_view.py](../jasper/active_speaker/crossover_v2/frequency_view.py), [gate_sweep.py](../jasper/active_speaker/crossover_v2/gate_sweep.py), [forward_model.py](../jasper/active_speaker/crossover_v2/forward_model.py), [distortion.py](../jasper/audio_measurement/distortion.py), [snr_policy.py](../jasper/audio_measurement/snr_policy.py) |
| Room | [room_selection.py](../jasper/active_speaker/crossover_v2/room_selection.py), [room_views.py](../jasper/active_speaker/crossover_v2/room_views.py), [room_prescription.py](../jasper/active_speaker/crossover_v2/room_prescription.py), [room_grade.py](../jasper/active_speaker/crossover_v2/room_grade.py), [room_limits.py](../jasper/audio_measurement/room_limits.py) |
| Candidates and saving | [candidate_parts.py](../jasper/active_speaker/candidate_parts.py), [measured_crossover_candidate.py](../jasper/active_speaker/measured_crossover_candidate.py), [baseline_profile.py](../jasper/active_speaker/baseline_profile.py) |
| Native bass | [dynamic.py](../jasper/bass_extension/dynamic.py), [dynamic_graph.py](../jasper/bass_extension/dynamic_graph.py) |

Verified PR context on the date above:

- [#4758](https://github.com/jaspercurry/JTS/pull/4758), merged: native dynamic
  bass, shared saved-layer composition, validated native descriptors, removal of the
  standalone bass runtime, bench, and sealed-only fitter. Start here.
- [#4700](https://github.com/jaspercurry/JTS/pull/4700), merged through #4758:
  save measured Room candidates through the existing apply transaction.
- [#4684](https://github.com/jaspercurry/JTS/pull/4684), merged: full-band Room
  capture below speaker analysis floors. A quality floor is not a driver cutoff.
- [#4657](https://github.com/jaspercurry/JTS/pull/4657), merged: exact captures,
  candidate predictions, and evidence views. Extend this evidence reuse.
- [#4773](https://github.com/jaspercurry/JTS/pull/4773) and
  [#4774](https://github.com/jaspercurry/JTS/pull/4774), merged: recent tuning
  disclosure and comparability fixes. Start from current main.
- [#4660](https://github.com/jaspercurry/JTS/pull/4660) and
  [#4643](https://github.com/jaspercurry/JTS/pull/4643), still open: older
  scheduled-model candidate and bench work. Inspect any useful analysis, but
  do not merge wholesale and restore the removed architecture. Identify
  superseded work and arrange closure through the normal process.
- [#4768](https://github.com/jaspercurry/JTS/issues/4768), open: three copyable
  program prompts. Finish at the end, with the runbooks they point to.

## Contracts between programs

| Program | Baseline playback | Candidate owns | Earlier evidence |
| --- | --- | --- | --- |
| Speaker | Declared base: routing, crossover, protection, applicable trims/alignment | Speaker filters and alignment | Compatible driver/summed captures, gates and angles |
| Room | Accepted speaker tune; bass extension off | Room filters and target | Speaker reference plus current listening-area captures |
| Bass extension | Accepted speaker plus Room; extension off | Added-bass shape and volume/demand taper | Room response, known driver facts, new bass level tests |

Preference EQ stays out of measurement baselines. Temporary trials retain the
saved stack and restore normal playback. Record the graph actually played.
Changing Room must not rewrite speaker tuning; changing bass must not rewrite
either upstream layer. An upstream change retains old evidence with its scope
and discloses which downstream validation describes the old stack. It does not
create a new global parking rule.

One measurement record owns take/WAV identity, candidate and played graph,
speaker/side, microphone calibration, pose, stimulus, volume, and integrity.
One candidate owns proposed coefficients, bass settings, and evidence links.
One applied snapshot owns the accepted stack. Extend these records only where
a demonstrated comparison needs a missing field.

## Reuse evidence across sessions

Start with status/inventory and the applied candidate's evidence links.
Discover retained rounds before opening another measurement session. Read
metadata first; load selected audio only when needed. Do not create a history
database or use chat prose as measurement truth.

- Separate matched before/after comparisons from diagnostic comparisons.
  Matched comparisons need compatible pose, calibration, level, stimulus and
  setup, with the intended filter change identified. Diagnostics may deliberately
  change position, distance or gate; name the changed variable.
- Re-window the same raw capture to study gate sensitivity. Store its window,
  valid band and source take. This is a view, not a new take. Short gates cannot
  establish low bass; longer gates admit room energy.
- Compare gated speaker and ungated Room trends over their common reliable
  band. Show level/distance alignment separately from shape. Moving a speaker
  into its listening location changes the acoustic setup: a raw subtraction
  is not an isolated room transfer function. Gates and locations inform a cause;
  they do not prove one.
- Account for filters already present in each recording. Reuse the existing
  source/target graph-ratio prediction where valid; do not stack replacement EQ
  onto already corrected evidence. Dynamic processing also needs signal and
  level context, so a static ratio alone is insufficient.
- Repeated takes at a pose do not count as extra positions. Preserve valid
  takes after interruptions and repeat only affected work. Missing context means
  limited/unknown comparability, not erased history.

## Delivery sequence

### 1. Reusable evidence and bass analysis

Replay the retained campaign below through normal analysis owners. Move only
missing, proven operations out of temporary scripts. Add a compact bass view
through existing inventory/round-views, with arrays in the artifact. Reuse
frequency, smoothing, SNR and distortion calculations.

For each band, level, pose and candidate report fundamental output, added gain,
harmonics, noise qualification, repeat variation, and input/output compression
where known. Include absolute harmonic level when calibrated, not just ratios.
Compare the same qualified frequency bins. Retain harmonic coverage beyond the
stimulus band where the acquisition/reference supports it; missing coverage is
unknown, not zero distortion.

Separate actual DSP drive from requested boost. Use the exact graph and native
offline DSP replay or existing output evidence to account for deliberate
compression. Disclose unknown contributions when unavailable; do not add an
on-device monitoring daemon. A +3 dB drive change with +1 dB fundamental output
is 2 dB combined compression to investigate, not a proven excursion limit.

Done: a new session reproduces useful campaign comparisons without private
analysis code and can identify only the missing evidence it needs.

### 2. Room fit and the reusable measurement batch

Keep the existing Room path; simplify friction found by replaying its public
flow. Fit a few filters to the spatial trend and explicit target, showing
per-position regressions. Use speaker, gate and position evidence when judging
broad deficits. A persistent dip alone does not justify arbitrary inversion.

The current Room path is ceiling-limited. Make its prior speaker reference
explicit. Any broad higher-frequency correction needs a distinct, speaker-
informed rule and a measured trial; do not silently widen bass rules or promise
full-band fitting that is not implemented. Detailed reflection/directivity
inversion stays outside this release.

Extend the existing plan/batch model only as needed for ordered poses, candidate
choices, stimulus band/duration/repeats, and level settings. Plans are data,
not another script language. Keep eleven default positions, quick center/-20/+20,
and optional placement headline/detail. Counts follow the position list.
Capture purpose and microphone mover remain separate.

Batch useful candidates and levels at each held pose under the existing start
action. Humans normally move the mic; the arm is a test adapter. Include short
quiet recording and repeat options when they answer noise/repeatability questions.
Reuse admission, cancellation, and restoration.

Done: Room baseline -> proposal -> trial -> comparison -> save works through
public tools and preserves the speaker tune. Bass uses that same capture path,
including all three positions for this hardware campaign.

### 3. Small empirical bass fitter and dynamic validation

Start with the accepted Room response and requested smooth bass target.
Predict a small number of useful shapes, then measure promising choices.
Correct remaining Room peaks in Room, then fit extension to that baseline.
Avoid a large arbitrary boost grid.

Use short focused sweeps or existing supported bursts, raising drive in small
steps while useful. Adapt by band: target reached, quality worsened, or evidence
unclear. These are findings and experiment advice, not permanent permission
gates or a universal distortion threshold.

Test two control axes separately: hold Main and bass reference while varying
stimulus demand, then vary canonical volume with a fixed stimulus. Record both.
Select compact boost shape and smooth taper from the tested range. Extend the
current descriptor/native graph only where fitting requires it; retain one
descriptor owner and the existing volume coordinator. If the fixed native
shelf cannot fit the required shape, add the smallest native filter representation
that can, with graph/admission tests.

Keep the original signal when extension recedes. Inspect transitions with a
short varying-demand signal, full-range captures, and listening. Do not claim
an 80 dB seat test proves maximum output at every frequency or in every room.
Store the tested scope with the candidate. Unknown enclosure details are
disclosed; they do not force a sealed model or a nearfield prerequisite.

Done: select and save a supported preset through public tools, with measured
bass gain, distortion tradeoffs, and volume/demand taper.

### 4. Entry points, runbooks, and final cleanup

Give Speaker, Room and Bass a short canonical runbook entry: inputs, discover/
reuse, plan, measure, analyze, trial, save, and recover. Use linked pages or
anchored sections of the existing runbook, whichever is smaller. Shared rules
stay in one common owner. Expose tools in inventory/help and regenerate the
existing tool menu as needed.

Finish #4768 with three concise copyable prompts using current device identity
and the correct runbook. No hardcoded JTS3 address or credentials in the product.
Keep placement text configurable; keep DSP details out of the measurement
screen unless they help the user choose an action.

Replace conflicting instructions in scope, including the old seven-position
description. This plan corrects the overstated Microsoft implementation claim.
Retire obsolete bass-wave documents and callers once verified superseded;
preserve research/decisions in existing homes. Do not add more handoff files or
a growing progress ledger. Test entry/recovery behaviour, not copied prose.

Done: an LLM without chat history starts each program, finds relevant earlier
evidence, and completes a measured trial through the same small toolbox.

## Existing evidence and hardware constraints

Local evidence pointers; do not bulk-commit these recordings into Git:

- Bass report/data:
  `/Users/jaspercurry/.codex/visualizations/2026/09/09/01a08704-1f17-7f70-b3ac-69a0a34cac4c/`.
  Start with `bass12-measurement-report.md`, `bass12-measured-summary.json`,
  `bass12-applied-proof.json`, and `bass12-excluded-captures.json`.
- Raw bundles and experimental analysis:
  `/private/tmp/jts3-bass12-20260910/`. Inspect `assess_quiet.py`,
  `analyze_bass12.py`, `plot_bass12.py` and native DSP replay artifacts for
  proven operations to move into shared owners. Some import old worktrees.
- Earlier speaker evidence:
  `/Users/jaspercurry/.codex/visualizations/2026/09/08/01a07f0a-b2ff-7e61-8f77-6f317f239cd2/jts3-tuning/`.
  Recover raw records through banked history where available. The tasks
  "Implement reusable LLM tuning workflow" and "Review speaker tuning workflow"
  provide context, not numerical authority.

The last campaign compared extension off/6/12 at center and +/-20 degrees,
with two volume settings and separate focused captures. It established useful
gain and taper, not a completed flat bass fit or physical output limit. Below
50 Hz paired evidence was noise-limited. Received H3 rose with boost; it is not
isolated driver THD and is not proven inaudible. The ambulance-affected left
batch was excluded and repeated; use accepted records.

The last verified JTS3 setup used an Epique E150HE-44 woofer and 7-inch Epique
E180HE-PR. The arm moves the microphone; the speaker stays fixed. Use center
and +/-20 degrees for this campaign, never beyond +/-45. Preserve the saved
speaker tune. Removing crossovers or declared protection is outside this plan.
Do not silently reintroduce the removed blanket 40 Hz high-pass as a measurement
floor.

Earlier user authorization covers JTS3 deployment and short low/moderate-level
trials up to 80 dB SPL, with margin for stop delay. Read current live state and
confirm identity/setup before playback; earlier snapshots are not current
state. Respect AGENTS.md non-negotiables and physical placement/start actions.
Do not overlap hardware tasks. Dynamic-bass group playback remains unsupported
until canonical volume reaches every endpoint; per-speaker fitting still works.

## Validation and completion

Use small, single-concern changes along these boundaries. Replay first, then
hardware. Follow current review tiers and test-fast/test-merge requirements;
DSP output math needs the additional adversarial review. CI must pass before
merge. Deploy only with `scripts/deploy-to-pi.sh`; check running SHA, normal
status/doctor surface, and saved/live tune.

Pin meaningful behaviour: preserved upstream layers, exact take/graph identity,
repeat versus position counting, calibration/level comparison disclosures,
unknown low-SNR results, harmonic coverage, volume/demand taper, cancellation
restoration, and reload of the saved candidate. Use small replay fixtures and
native DSP tests, not all campaign WAVs or broad tree-scanning checks.

Finish with three-position measured before/after, tested levels and coverage
gaps, the saved chosen candidate, restored normal volume, and arm at center.
Reuse sufficient existing evidence; no ceremonial final round is required.
If hardware is unavailable, finish code, replay, review and concrete trial
preparation, and name that one remaining blocker.
