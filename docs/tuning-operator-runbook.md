# Tuning toolbox — optional operator runbook

## Entry contract

1. Read `jasper-crossover-prescriber status` to list recent retained rounds.
   Select one with `status <round-dir>`, then read its `inventory`. Bare status
   leaves evidence unselected. Follow returned paths and the latest rationale
   note; `/sound/measurements/` also shows retained history.
   For a fresh linearization, inspect the basic structure/trim profile and
   measure the temporary base graph. To resume, keep the current tune, select
   its relevant round, and start from the unresolved question. Incomplete older
   evidence keeps its recorded scope; it does not require a reset or universal
   base recapture.
2. Code owns calculations, graph composition, protected capture, and evidence.
   The LLM chooses experiments and interprets results. The human places the mic,
   starts each pose batch, reports physical changes, and judges listening.
3. Speaker starts from the base tune. Room starts from the accepted Speaker
   tune with Room and bass off. Bass starts from accepted Speaker plus Room,
   with extension off. A candidate adds only its layer's proposed change.
   Measurement excludes preference EQ. Temporary playback restores the saved
   stack and volume; save is a separate action.
4. Inspect and enrich a round before freezing its packet. Use that exact packet
   for both `propose` and `stage`. New evidence needs a new snapshot and a
   prescription bound to it.
5. Each round can be the last. Continue only when more evidence would help;
   there is no campaign count, plateau gate, or required final round. A restored
   loser remains available for analysis and useful parts.
6. Adoption is explicit and names the candidate. Report what was measured,
   coverage gaps, failures, and the next human action. Never simulate a mic walk
   or claim an unrun combination has been verified.

The [doctrine](measurement-loop-doctrine.md) owns layer and authority rules.
The [methodology](tuning-methodology.md) provides optional science guidance.
Use a tool's `--help` for its current fields and limits. Full evidence belongs
in artifacts; stdout is the compact answer.

## Speaker

Inputs are the current driver declarations, base design, microphone calibration,
and retained Speaker evidence. Fit reliable driver response, then compare the
complete crossover sum and nearby positions. Use gates only where their valid
band supports the claim. Choose the order from the next question.

1. **Orient.** Read `jasper-crossover-prescriber status <round-dir>` for a known
   round. Confirm the speaker, base design, and returned paths.
2. **Plan the question.** For an initial baseline, use the base tune without
   inherited corrections. For a comparison, name the candidate fingerprints
   and required poses. `jasper-angle-capture plan` previews the program and
   human effort without staging it.
3. **Stage and measure.** Register the wired measurement mic once. Its record
   and its calibration files live under a root-owned, group-`jasper` state
   directory the login account is outside, so on the speaker every verb runs as
   `sudo /opt/jasper/.venv/bin/jasper-mic-calibration <verb>`:
   `fetch --model <key> --serial <serial>`, or `upload <file>`. `show` prints
   the household record every take's calibration context resolves from, and a
   box without one measures uncalibrated. Stage the chosen program, then use
   `jasper-round open --tier express` (or the chosen tier) and `jasper-round wait`.
   Give the human the returned `handoff_url` and the next placement/start action.
   `scripts/run-crossover-round.py` is the laptop adapter for one round.
4. **Bank.** `jasper-round bank <session-dir>` uses the `session_dir` returned
   by `wait`. It places the round under
   `/var/lib/jasper/active_speaker/campaigns/<round-id>/`, outside normal session
   retention. Keep completed valid takes even when another take fails.
5. **Inspect and enrich.** `jasper-round-views inventory <round-dir>` shows
   available artifacts and commands for analyses the round can produce. Run
   relevant views, such as
   `classify-features`, `distortion`, `directivity`, or `frozen`, before freezing
   evidence for a prescription. `per-seat --include agreement` shares its
   round read and seat preparation; add `directivity` or `co-metrics` only
   when useful. Each selected result retains its own outcome, coverage and
   detail path, including failures. Inventory commands quote known inputs;
   fill listed `required_inputs` before use. A missing banked pose index does
   not offer a re-banking command.
6. **Freeze.** `jasper-crossover-prescriber packet` writes `packet.json` and
   prints its path, fingerprint, availability, and `rebuild_status_command`.
   That command repeats the supplied inputs, including `--state`; omitting
   an input can change the fingerprint without any saved evidence changing.
   Select the round using the paths/flags in `status` or `packet --help`. This file is a snapshot:
   enriching the source round later does not change what it contains.
7. **Propose and stage.** Read `jasper-crossover-prescriber contract --round <dir>`
   for speaker, room, and bass schemas and bounds. Use `--packet <round-dir>/packet.json` for both
   `propose --prescription -` and `stage --prescription -`. Stage against the
   live state file, not a banked `state.json` copy. If the evidence changes,
   freeze a new packet and author the corresponding new prescription.
8. **Decide.** Compare measured outcomes for the stated goal. Finish with the
   least-bad measured candidate, or compile another candidate and reuse the same
   measurement tools. The latest round may already supply enough evidence.
9. **Adopt when chosen.** `jasper-round apply --expected-fingerprint <fp>` is
   explicit persistence of the selected candidate. On the laptop,
   `scripts/run-crossover-round.py --apply <fp>` is a separate invocation.
   If a post-apply check would answer a remaining question, use
   `jasper-round open --stage post_apply`; it is not a compulsory final round.

## Candidate batches and reusable parts

A tournament holds the declared crossover, filter family, slope, and topology
fixed. Candidates can differ in their supported corrective filters, role trims,
delay, and polarity. Use the existing design path for a different base design;
its driver protection must be derived for that design.

```
jasper-angle-capture stage --program tournament --size express --candidates base,fp1,fp2
```

`base` adds the base graph to the same pose batch. Choose the batch size for the question.
At a pose, the execution owner runs each candidate in order under one placement
and start grant. The human sees “Config 2 of 3 — keep the mic still.” A new pose
or explicit recovery/retake requires another human action. Do not emulate this
by repeatedly adopting profiles between takes.

Opening a measurement session consumes any pending correction prescription for
a possible newly fitted candidate. Named tournament candidates use their saved
graphs; they do not require that pending prescription to be cleared.

Read each take's played graph/config fingerprint, candidate, pose, capture
settings, level, calibration context, take identity, and status. Full candidate
playback matters: shared trims or alignment-only playback cannot establish the
effect of a candidate's filters. Combined-speaker questions need summed captures;
matching solo magnitudes do not rule out a delay/polarity difference in the sum.
Solo complex responses remain useful for prediction and diagnosis.

A driver prescription replaces the filter chain for each role it names.
Omitting a filter in that role removes it; read the `displaced_filters`
disclosure. `pinned_trim_db` is an optional role map, with values in −60 to 0 dB
for roles the document prescribes. It is not a requirement to recalculate every
candidate's trim. `Peaking`, `Highshelf`, and `Lowshelf` follow the emitter's
supported ordering; read `jasper-crossover-prescriber contract --round <dir> --section speaker` for bounds.

To combine A's woofer change with B's tweeter change, use:

```
jasper-crossover-prescriber compose --base <fp> --role woofer=<A> --role tweeter=<B>
```

The result carries the source references. State the expected effect separately from parent
observations. C has a new identity and is unmeasured. Measure it with the same
batch tools only if that result would change the decision. Co-varied parent
changes support a hypothesis, not proof of which part caused the result.

`jasper-round apply --expected-fingerprint <fp>` selects a banked same-design
candidate when needed, then uses the normal apply path. An authored candidate
needs an intact capture of its own complete graph. Selecting it does not copy
its parents' measurement claims. The basic
profile has its own explicit `jasper-basic-profile review|apply` door; replacing
a saved tune is not necessary just to make a temporary baseline measurement.

## Optional visual and window diagnostics

Use this step when a curve shape could change the next experiment. The LLM
must render **and open/view the image** before interpreting it. Writing a plot
or reading its numbers alone does not complete visual inspection.

```
jasper-round-views frequency <before-take.json> <after-take.json> --image /tmp/before-after.png
jasper-round-views sweep <round-dir> --scope take --set <set-id> --take <exact-take-id> --rungs-ms 2 4 7 12 --image /tmp/windows.png
```

Image rendering runs on the laptop with the optional `plots` install extra.
Both commands also save the shared frequency-view JSON. `--series a:<id>
b:<id>` selects exact curves from that JSON. Keep candidate, played graph,
take, pose, reference, window, smoothing, and valid coverage visible. An absent
field is unknown. The zero line is the stated flat display reference; it is
not an absolute SPL target. Shared references retain branch level differences.

After viewing, describe the peaks, dips, broad tilt, crossover shape, and pose
differences that are visible. Then state possible causes as hypotheses, with
the evidence that could separate them. End with one useful next experiment,
or explain why the measured result is enough. Do not prescribe EQ from a dip
alone or treat a score as proof of the cause.

`sweep --scope take` reads one exact WAV/program binding and shows its impulse beside
alternative windows. The round sweep calculation owns its grid,
smoothing, taper, and common reference. It does not replace the saved verdict.
Raw impulse diagnostics have no microphone correction; preprocessing states
clock correction and timing coordinates. Longer windows admit more room.
Window resolution alone does not prove a reflection-free result.

For pose statistics, use `sweep <round-dir> --scope round --candidate <fp> --graph <played-fp>`.
The round scope reads every banked summed capture; `--set` limits it to one
manifest set. Mixed candidate/graph records are refused, even at the same pose. Use exact
take window overlays when only one recording should answer the question.

## Optional complete-tune branch check

Use one saved candidate when the crossover sum needs a closer look:

```
jasper-angle-capture plan --program branches --size express --candidates <fp>
jasper-angle-capture stage --program branches --size express --candidates <fp>
```

The branch program starts after a full candidate is banked. It cannot select
the bare base graph; keep using the normal baseline path until a candidate
exists.

Open the usual browser session and give the human its placement/start action.
At the selected pose, one recording contains woofer, tweeter, two clock-check
repeats, then both. All five sweeps use the same stimulus level and volume.
Each branch retains the candidate's crossover, correction, trim, delay,
polarity, and protection. This is a diagnostic; it does not fit or adopt a tune.
The existing `--regime both` remains the neutral per-driver/summed pair.

The retained take carries all three complex curves, raw impulses, exact
candidate/graph/WAV/program identities, and clock/gate facts. View it with
`frequency <take.json> --image /tmp/branches.png`. Use
`sweep <round-dir> --scope take --set <set-id> --take <take-id>` on that exact
take with `--role woofer`, `tweeter`, or `summed` to inspect window sensitivity.
Do not align each impulse to its own peak before comparing driver phase.

When useful, read acoustic dependencies in this order: capture validity first,
because an invalid take cannot support a comparison; branch balance before
timing and polarity, because a weak branch limits cancellation; gates and poses,
because a moving feature may come from the room or placement; then a candidate
trial, because a forecast becomes evidence only when the changed sum is
measured. Skip evidence already answered by a valid compatible take.

Run `jasper-round-views forward-model <round-dir> --set <set-id> --take <id>` without a
candidate to check whether woofer plus tweeter reconstructs the same take's
measured sum under one common window; `--window-ms` selects a disclosed
alternative. This closure checks the model. Before a useful changed-candidate
trial, save the forecast separately:

```
jasper-round-views forward-model <round-dir> --set <set-id> --take <id> \
  --candidate-json <full-candidate> --out forecast.json
```

Poor same-take reconstruction or window closure weakens the forecast; use
measured full-sum comparisons for the decision.

The code resolves source/target metadata and timing, optionally through
`--basis-candidate-json` or `--candidate-root`. Use the `branches` program for
the target candidate's validation trial at the same pose. That trial records
the target branches and sum. Compare its exact sum without overwriting the
forecast:

```
jasper-round-views forward-model <round-dir> --set <set-id> --take <id> \
  --candidate-json <full-candidate> --measured-round <trial-round> \
  --measured-set <trial-set-id> --measured-take <trial-id> \
  --expected-prediction-fingerprint <forecast.summary.prediction_fingerprint> \
  --out comparison.json
```

Do this only when it can change the decision, not for every pose or as a
required round. Stdout stays compact; the full curves remain in the saved
artifacts.

`delay-landscape <bundle-dir> --phase lateral --take-path <indexed-take-path>
--fc-hz <corner>` reuses the existing complex-sum/null calculation. With these
curves, its signed delay is a **residual addition to the measured tune**;
the tune's physical and DSP delay is already present. A positive residual
delays the tweeter relative to the woofer. Add it to the saved signed alignment
when authoring a full candidate variant. Confirm variants with `tournament`.
`jasper-null` plays neutral branches and cannot confirm those full-tune changes.

A null needs valid coverage on both sides of the crossover. A short gate can
remove a required shoulder and yield no depth. Inspect the saved gate/floor
and impulse, try an explicit alternative window as a disclosed room-inclusive
diagnostic, or move the mic to delay the first reflection. If the available
span still cannot answer, retain that limitation and use measured full sums.

## Room

Inputs are the accepted Speaker candidate, its gated reference and trusted
frequency band, the actual listening placement, and an explicit room target.
Record the Speaker reference even when older captures lack its identity.
Measure the seat cloud through Speaker with Room and bass off, ungated
(methodology §11). The current fitter corrects below its disclosed ceiling;
higher-frequency deficits need separate speaker-informed evidence. In order:

1. `jasper-angle-capture plan --program room`: the default listening-area
   cloud. `--size quick --mover arm` selects the three-position arm smoke test;
   the speaker stays fixed and the arm moves the microphone. `stage` the plan.
   The arm test samples its local area, not the final listening-area cloud.
   Existing `seat/cloud`, `seat/cube` and `seat/express` remain available.
2. `jasper-round open --tier express`, then the phone's position-ready walk
   states each place from the head centre at ear height.
3. `jasper-round bank <session-dir>`: records carry the measurement purpose,
   actual pose and gating result. Room analysis retains reflections.
4. `jasper-round-views room <round-dir> --set <set-id>` writes the set's
   `room.json` artifact with a set suffix. It includes ceiling and provenance,
   median, spread, position deviations, persistence with boost admission,
   cut and boost limits, the incumbent room identity, and the boundary prior.
   Missing geometry has a reason code. Repeats count once per pose, and curves
   use only shared measured coverage. The manifest owns the set selection.
5. `jasper-crossover-prescriber propose <round-dir> --prescription <doc>`
   judges a room prescription (`kind: jts_room_prescription`) against
   the room document's median; `compose --base <applied fingerprint>
   --room-prescription <doc> --room-median <path>` banks the room candidate;
   stage the same `jasper-angle-capture plan --program room
   --candidates <fingerprint>` walk, then open and bank a new round. Each seat
   capture plays the room candidate through the accepted speaker tune.
6. `jasper-round-views room-grade <round-dir> --set <candidate-set>
   [--incumbent <set-id>]`: grade the room document against the incumbent set
   from the same run, or an explicit set. An unknown or ambiguous incumbent
   is disclosed. Comparisons use shared frequency coverage and
   disclose their level alignment and capture compatibility. A regressed band
   is a disclosure; restore follows the same adoption path.
7. Save the chosen measured candidate through `jasper-round apply
   --expected-fingerprint <fingerprint>`; inspect the saved stack and its evidence links.
   Confirm that Speaker filters and alignment remain as accepted. Use
   [Evidence and recovery](#evidence-and-recovery) to resume or restore.

Plan defaults and ordered positions live in
[`measurement_plans.json`](../jasper/active_speaker/measurement_plans.json).
Edit a layout to change the number of positions; counts follow that list.
Optional pose `headline` and `detail` replace the derived screen text.
Capture purpose controls playback and analysis; the mover only controls placement.

`--program bass` uses the same cloud and quick positions, with the accepted
Speaker and Room layers playing and extension off. `--candidates` runs each
choice at a held position before asking for the next move. A repeated take
does not add a position to the analysis count.

For a focused batch at one held pose, `jasper-measure --specs <json>` accepts
an ordered list of `MeasureSpec` objects. Use `sweep_band_hz`, `sweep_s`, and
`level_ladder_dbfs` for the band, duration and stimulus levels; repeat a level
to check variation. Declared driver limits still cap duration and level.
The normal program retains a quiet prelude for noise analysis. Keep canonical
volume fixed for demand tests; use separate `--volume-db` runs with a fixed
stimulus for volume tests. Each invocation restores the prior playback state.

These views do not authorize correction above the current ceiling.
If the speaker's trusted floor exceeds that ceiling, the gap remains ungraded;
use a longer valid gate or another suitable measurement to assess it.

## Bass

Inputs are accepted Speaker plus Room, a smooth bass target, and any compatible
retained bass captures. Start with `status` and `inventory`; correct remaining
Room peaks in Room before fitting extension.

1. Plan `jasper-angle-capture plan --program bass`. The shared cloud is the
   normal listening-area plan; `--size quick` gives the smaller trial. Stage
   useful off/candidate choices at each held position, then open and bank a
   round through the same placement/start flow as Room.
2. Run `bass` and `bass-compare` below. Inspect frequency plots, noise,
   harmonics and repeat variation before deciding which bands need more data.
   `bass-fit-table` suggests a bounded native shape from matched measured changes.
3. Compose against the accepted Room candidate with `--bass-extension-json`
   and linked `--observation-ref` evidence. Trial useful changes with short
   focused sweeps and small input steps. Hold volume fixed while varying
   stimulus demand; hold stimulus fixed while varying canonical volume.
4. Use native replay when deliberate DSP reduction is unclear. Inspect a short
   varying-demand signal, a full-band capture and listening for transitions.
   Record the tested levels, positions, gain and harmonic tradeoffs. Uncertain
   results suggest a next measurement, not a physical output limit.
5. Save the chosen measured candidate with `jasper-round apply --expected-fingerprint
   <fingerprint>`. Check the saved Speaker and Room layers, normal playback,
   restored volume and microphone position. Reuse sufficient existing evidence;
   another full round is optional. Recovery uses the shared section below.

### Bass analysis tools

`jasper-round-views bass <round-dir> --set <set-id> --calibration-root <copied-registry>`
replays retained summed captures on the laptop. It writes `bass_view-<set-id>.json`
with exact take references, frequency curves, H2/H3, and quiet-window noise
estimates. Use only frequency bins qualified in both takes for comparisons.
Missing harmonic coverage, absolute SPL context, or actual DSP drive stays
unknown. These received measurements do not establish a driver output limit.

Compare sets with `jasper-round-views bass-compare <before-round>
<after-round> --before-set <id> --after-set <id> --change candidate`.
The unique on-axis take is the default; `--before-take` and `--after-take`
select another retained take within each set.
Use `--change demand` for a fixed-volume stimulus change, or `volume` for
fixed-stimulus volume tests. The result separates requested input change,
measured output change, and combined compression, including intended DSP
action. `diagnostic` permits changed setup with that difference disclosed;
it does not identify an isolated room response. Missing context stays visible.

`bass-fit-table <round-dir> --run <run-id> --candidate <candidate.json>
--target <target.json> --tolerance-db <db>` fits each recorded operating level.
The target contains `freqs_hz` and relative `magnitude_db`; repeat `--candidate`
for each candidate in the run. Pairs come from the manifest at matching levels
and poses. Use full-band baseline captures: `--reference-band-hz` defaults to
300–1000 Hz. The table discloses the recorded Main, Aux1 and program identity,
and the native loudness boost at each Aux1 setting.

The result compares off, the fitted boost, and the measured boost. It uses
one-third-octave smoothing and equal position weights; repeats report variation.
It fits only shared qualified bins and never exceeds the measured boost.
Intermediate response predictions are approximate because the native compressor
also depends on the signal. The descriptor retains the measured taper settings.
Compose it against the accepted Room candidate, link the fit and comparisons
with `--observation-ref`, then measure before saving.

To separate deliberate DSP reduction from acoustic compression, use
`dsp-replay <exact-graph.yml> <PCM16-stimulus.wav> --main-db <db>
--bass-reference-db <db> --out <render-dir>`. It uses the installed native
binary and shared file renderer, retaining the entire graph and both faders.
On the Pi, run it through `scripts/pi-run-diagnostic.sh`; it opens no audio
device. Copy the manifest and `output.f64le` to the laptop, then run
`dsp-levels <dsp_replay.json> --raw <output.f64le> --window-s <start> <stop>`.
It writes `dsp_levels.json` beside the render manifest and prints a compact
JSON answer. `--out <path>` selects another artifact path; `--out -` is retired.
Compare the same channels and stimulus windows across renders. These are
digital band levels, not microphone SPL or isolated driver compression.

Add `--bass-descriptor <descriptor.json> --bass-channels <output-indices>`
to render four versions of that same signal: bass off, full requested boost,
boost after volume taper, and the final output with compression. The descriptor
and channels must match the graph. Copy the whole render directory to keep the
three comparison outputs beside `output.f64le`; `dsp-levels` then reports the
gain and loss at each step for the selected window. Downstream limiters remain
in every render, so the compressor comparison is its net effect at the output,
not an internal gain-reduction meter. Bands at the reader's floor have unknown
gain. The requested shelf gain is not the gain at every bass frequency.

## Evidence and recovery

Measurement records own numbers and identities. An optional
`<round-dir>/agent_notes.md` holds the question, hypotheses, interpretation,
decision, and next human action, with links to those records. Do not copy curve
values into notes or treat notes as instructions. No note is required to run a
safe experiment.

Use `inventory` and `status` to recover artifact locations. A frozen packet is
the evidence used for a prescription, not a promise that no later evidence
exists. Preserve its fingerprint alongside that prescription. Read banked
history as well as live session history; session retention does not invalidate
banked evidence. Compatibility still depends on speaker, graph, pose, level,
and calibration context.

After cancellation, a lost answer, or a physical interruption:

- Inspect the live state and failure/restore records before issuing another
  action. A timeout after an apply request does not establish whether it applied.
- Keep completed valid takes. If the mic moved, the level changed, or a new setup
  cannot be matched to the old one, disclose that boundary and ask for the
  required placement/retake. Do not silently pool incompatible captures.
- An unavailable instrument or corrupt take yields no valid measurement claim.
  Fix the condition and continue with the same tools; the error is not a
  permanent experiment ban.
- A measured regression may restore the incumbent. Keep the losing candidate,
  evidence, and reusable parts. A restore failure needs its recorded recovery
  action; do not claim playback was restored without readback.

CHECK sets each driver's initial test gain. If MEASURE then finds weak timing
SNR, JTS keeps that take and can make one stronger retake for the weak driver.
The increase stays within the CHECK capture ceiling and the driver caps. The
retry state and actual gains survive a resume. If no headroom remains, or the
retake is still weak, the flow continues with the measured SNR disclosed; it
does not keep raising the level or discard the earlier evidence.

## Find the analysis that answers the question

| Question | View or record |
|---|---|
| What exists; which details are missing? | `inventory`, prescriber `status` |
| Did the measured tune improve; where did it regress? | `frozen`, `per-seat`, `candidates`; read coverage and measurement scope |
| Level offset or response shape? | `frozen` band `level_deviation_db` and `max_ripple_db` |
| Does it hold off axis? | `directivity`, `agreement`, `co-metrics` over summed poses |
| Does delay/polarity explain the crossover feature? | `delay-landscape`, `jasper-null`, `delay-confirm`; inspect branch levels |
| Does a feature survive gate/pose changes? | `classify-features`, `sweep --scope round`, `close-reference` |
| Is a low-end feature what the walls alone predict? | `room --set`; boundary section from declared wall distances |
| Is the distortion window valid? | `distortion`; inspect per-order window and overlap status |
| How stable is the measurement? | `repeat`, `repeat-floor`; distinguish random and systematic error |
| Which part of a prescription did cloud evidence constrain? | `cloud-binding` |
| How flat is the seat cube below the ceiling; did a room candidate move a band the wrong way? | `room-grade --set [--incumbent]` |
| Can the same-take branches reconstruct its sum, or what does a full candidate predict? | `forward-model`; select exact captures and treat prediction as unmeasured |
| Show a curve or compare two takes? | `frequency <A> [<B>]` |

The catalog at `/sound/measurements/` includes banked rounds and retained live
sessions. `frequency` can read a banked round, bundle, or take file directly.

## The tool menu

This block is generated from CLI help. Offline tools can write files.
Capture emits sound; apply persists a tune.

<!-- BEGIN GENERATED TOOL MENU (scripts/generate-tuning-tool-menu.py -- do not hand-edit) -->
| Tool | Does | Authority | Where |
|---|---|---|---|
| `jasper-basic-profile review\|apply` | Review and apply the basic profile -- the chosen crossover plus per-driver trim, delay and polarity, with no linearization and no blend correction, replacing the live tune and deleting no evidence. | mutating-with-gates | `jasper/cli/basic_profile.py` |
| `jasper-mic-calibration models\|fetch\|upload\|show` | Register the household's measurement microphone: fetch its vendor calibration by serial or store a file you already have, and remember that mic so every measurement resolves its calibration from one record. A box with no record measures uncalibrated. | advisory (`fetch`/`upload` write; `models`/`show` do not) | `jasper/cli/mic_calibration.py` |
| `jasper-seat-level` | Ramp the measurement volume until a calibrated mic at the seat reads the target dB SPL and bank it as the crossover session's measurement reference — PRECONDITION: `amixer -c <card>` shows the mic's capture control at 100%, where its Sens Factor is quoted, or every absolute SPL is wrong by the shortfall. | measured | `jasper/cli/seat_level.py` |
| `jasper-angle-capture plan\|stage\|show\|withdraw\|serve` | State one angle walk, see what it resolves to, leave it for the next measurement session, and serve it with the lab arm. | mutating (`stage`/`withdraw` write; `serve` moves the arm; `plan`/`show` are reads) | `jasper/cli/angle_capture.py` |
| `jasper-measure` | Measure this speaker once, bank the takes, print their ids | measured | `jasper/cli/measure.py` |
| `jasper-crossover-prescriber contract\|compose\|status\|packet\|propose\|stage` | Emit one crossover round's evidence packet, read a prescription back through the strict gate, and say where this speaker stands. | advisory (`packet`/`propose`/`compose` save artifacts; `stage` writes pending state; `status` reads) | `jasper/cli/crossover_prescriber.py` |
| `jasper-round open\|wait\|apply\|bank` | Open, wait on, apply and bank a crossover round from the speaker itself. The three wizard verbs scripts/run-crossover-round.py drives from a laptop, over the same transport and the same apply gate, plus the bank that files a finished session in the on-box campaign home. | mutating-with-gates (`open`/`apply`/`bank` write; `wait` does not) | `jasper/cli/round.py` |
| `jasper-round-views entry\|frozen\|repeat\|repeat-floor\|candidates\|agreement\|co-metrics\|directivity\|per-seat\|cloud-binding\|forward-model\|sweep\|frequency\|distortion\|dsp-replay\|dsp-levels\|classify-features\|findings\|close-reference\|delay-landscape\|delay-confirm\|room\|room-grade\|bass\|bass-compare\|bass-fit-table\|inventory` | Read a round's measured evidence. Select standalone views or per-seat --include agreement directivity co-metrics to share a round read. Answers use stdout; details use files. | advisory (analysis views save artifacts) | `jasper/cli/round_views/__init__.py` |
| `jasper-null` | Play the summed reverse null and bank one row per coordinate. Measures only; grades nothing. | measured | `jasper/cli/null_door.py` |
| `jasper-audition start\|stop\|status` | Play this speaker at a reduced DSP layer, then put it back | mutating (runtime only; durable graph untouched -- ADR-0193) | `jasper/cli/audition.py` |
| `jasper-declare-geometry set\|show` | Declare measurement rig geometry: speaker/mic heights, distance and optional ceiling, so entanglement_floor_hz has a provenance-labeled, non-measured source on rigs where the measured reflection finder structurally never fires (issue #3502); and optional front/side wall distances for jasper-round-views room. | advisory (`set` writes; `show` does not) | `jasper/cli/declare_geometry.py` |
<!-- END GENERATED TOOL MENU -->

Regenerate with `PYTHONPATH=. .venv/bin/python scripts/generate-tuning-tool-menu.py`;
`--check` verifies the committed menu. Tool code owns help, schema, and menu copy.

## URLs and access

Use the tool's `handoff_url`; it derives from the selected speaker's hostname.
The crossover surface is `/sound/speaker/crossover/` and records with the wired
Pi microphone; it needs HTTPS and trust in the speaker's local CA. Room has no
browser wizard — its steps are the CLI walk above.

Backend paths in tool output use `127.0.0.1:8770` on the Pi. Through nginx,
prefix crossover paths with `/sound/speaker`, for example
`POST https://<speaker>/sound/speaker/crossover/v2/republish`.
The laptop runner resolves `.env.local`; `--hostname` selects an explicit host.

## Debugging — where to look first

Read the named failure and its take/phase before changing code. Fetch the Pi logs
with `bash scripts/fetch-pi-logs.sh`; inspect `jasper-doctor --json` and the
speaker's `:8780/state`. Round evidence is file-based, not a `/state` round field.

| Symptom | First evidence |
|---|---|
| `locate_failed` or `channel_map_mismatch` | `event=program_analysis.anchor`, including ambiguity and capture identity |
| Graph or volume cleanup unclear | `session_volume_*`, `crossover_v2_round_restore`, `crossover_v2_round_recovery_required`, `crossover_v2_volume_close_failed` |
| Retake did not update the published result | `cloud_group_complete`, `cloud_spec`, `cloud_publish_skipped`; a repeated close is normal after a retake |
| Calibration differs or is unavailable | `crossover_v2_calibration_resolve_failed`, `crossover_v2_uncalibrated_capture` and the take's calibration record |
| Runtime refused a position action | Live position/take identity and `refusal_copy.py`; do not replay a stale grant |

`jasper/cli/_refusal.py` owns shared exit codes: 0 done, 1 refused, 2 unreadable,
3 unable to file the result. Read each tool's structured `reason`; argparse also
uses 2 for bad arguments. Geometry and laptop scripts have their own documented
codes. The round runner keeps sub-tool codes in its trail even when its own exit
code groups several failures.

During a measurement, household renderers can keep running while their lanes
are excluded from the output mix. A restart can interrupt measurement holds;
inspect the transition and capture validity rather than assuming clean resume.
Per-take memory, time, and excitation limits remain bounded. Use laptop-side
analysis for experiments that exceed the small Pi's budget.
