# Tuning toolbox — optional operator runbook

## Entry contract

1. Read `jasper-crossover-prescriber status` to list recent retained rounds.
   Select one with `status <round-dir>`, then read its `inventory`. Bare status
   leaves evidence unselected. Follow returned paths and the latest rationale
   note; `/sound/measurements/` also shows retained history.
2. Code owns calculations, graph composition, protected capture, and evidence.
   The LLM chooses experiments and interprets results. The human places the mic,
   starts each pose batch, reports physical changes, and judges listening.
3. Speaker linearization starts from the base tune. Every measurement graph
   excludes household preference EQ and room correction. A candidate adds the
   changes under test. Temporary playback retains household settings and
   restores normal playback; it does not adopt a saved tune.
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

## One possible flow

Choose the order from the evidence and the next question.

1. **Orient.** Read `jasper-crossover-prescriber status <round-dir>` for a known
   round. Confirm the speaker, base design, and returned paths.
2. **Plan the question.** For an initial baseline, use the base tune without
   inherited corrections. For a comparison, name the candidate fingerprints
   and required poses. `jasper-angle-capture plan` previews the program and
   human effort without staging it.
3. **Stage and measure.** Stage the chosen program, then use
   `jasper-round open --tier express` (or the chosen tier) and `jasper-round wait`.
   Give the human the returned `handoff_url` and the next placement/start action.
   `scripts/run-crossover-round.py` is the laptop adapter for one round.
4. **Bank.** `jasper-round bank <session-dir>` uses the `session_dir` returned
   by `wait`. It places the round under
   `/var/lib/jasper/active_speaker/campaigns/<round-id>/`, outside normal session
   retention. Keep completed valid takes even when another take fails.
5. **Inspect and enrich.** `jasper-round-views inventory <round-dir>` shows
   available artifacts and missing analyses. Run the relevant views, such as
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
7. **Propose and stage.** Author a prescription using the packet's response
   formats and `propose --help`. Use `--packet <round-dir>/packet.json` for both
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
supported ordering; use the generated response format for bounds.

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

## Room

The room is measured on the seat cube, through the applied tune, ungated
(methodology §11). In order:

1. `jasper-angle-capture plan --program seat --size cube`: seven summed stops
   around the listener's head (`--size express`: three). `stage` it; a human
   moves the microphone, and an arm cannot reach the seat.
2. `jasper-round open --tier express`, then the phone's position-ready walk
   states each place from the head centre at ear height.
3. `jasper-round bank <session-dir>`: the banked seat takes carry `pose_kind`,
   `seat_offset_m` and `gating_applied: false`.
4. `jasper-round-views room-ceiling <round-dir>`: the applied candidate's
   trusted floor, clamped; with no readable profile the default is used and
   disclosed.
5. `jasper-round-views room-median <round-dir>`: median, spread and
   per-position deviation below the ceiling; `room_median.json` is the input
   the room candidate reads.
6. `jasper-round-views room-persistence <round-dir>`: which peaks and dips
   hold across the cube, and at what fraction of positions.
7. `jasper-crossover-prescriber propose <round-dir> --prescription <doc>`
   judges a room prescription (`kind: jts_room_prescription`) against
   `room_median.json`; `compose --base <applied fingerprint>
   --room-prescription <doc> --room-median <path>` banks the room candidate;
   `jasper-measure --graph-scope room_candidate --candidate-id <fingerprint>`
   plays it for its trial through the accepted tune.

Nothing above the ceiling changes on this evidence.

A bass rung plays the same way, at the seat and through the same accepted
tune: `jasper-measure --graph-scope bass_candidate --candidate-id <fingerprint>
--bass-target-id <target> --level-dbfs <rung>...` installs that one member of
the candidate's bass family, over that candidate's own room set. Before any
audio it predicts each ladder step's seat SPL from the banked
`jasper-seat-level` reference, with the whole of the rung's boost added, and
refuses the request entire — never truncated to its quiet steps — when any step
reaches this box's commissioning ceiling (`bass_ladder_spl_ceiling`), when no
reference with a recorded stimulus is banked to predict against
(`bass_ladder_reference_unbanked`), or when the rung itself does not resolve
(`bass_ladder_candidate_unbanked`, `bass_ladder_target_unknown`).

`jasper-round-views bass-ladder <round-dir> --target-id <target>` then grades
those steps from the lowest banked level up and writes
`bass_ladder/<target>.json` beside `bass_fit.json` — the level the rung proved
(`max_level_db`), and the step that ended the ladder. The rung's own margin
policy supplies every constant; a step with an incident, with no harmonic order
clear of the measurement floor, over the policy's THD ratio, short of the
stimulus step by more than its compression limit, or reached over a gap larger
than one rung step ends the ladder there. A failed document is written too, and
the prescription door refuses the rung on it exactly as it does on none.

The ladder is graded only where the rung's boost and the step's own stimulus
meet. The summed sweep a rung plays today starts at the crossover's low bound,
which on a two-way sits above the corner an extension rung moves, so the view
refuses (`bass_extension_ladder_incomplete`, naming both bands) rather than
publishing a verdict about a band nothing excited.

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

## The tool menu

This block is generated from CLI help. Offline tools can write files.
Capture emits sound; apply persists a tune.

<!-- BEGIN GENERATED TOOL MENU (scripts/generate-tuning-tool-menu.py -- do not hand-edit) -->
| Tool | Does | Authority | Where |
|---|---|---|---|
| `jasper-basic-profile review\|apply` | Review and apply the basic profile -- the chosen crossover plus per-driver trim, delay and polarity, with no linearization and no blend correction, replacing the live tune and deleting no evidence. | mutating-with-gates | `jasper/cli/basic_profile.py` |
| `jasper-seat-level` | Ramp the measurement volume until a calibrated mic at the seat reads the target dB SPL and bank it as the crossover session's measurement reference — PRECONDITION: `amixer -c <card>` shows the mic's capture control at 100%, where its Sens Factor is quoted, or every absolute SPL is wrong by the shortfall. | measured | `jasper/cli/seat_level.py` |
| `jasper-angle-capture plan\|stage\|show\|withdraw\|serve` | State one angle walk, see what it resolves to, leave it for the next measurement session, and serve it with the lab arm. | mutating (`stage`/`withdraw` write; `serve` moves the arm; `plan`/`show` are reads) | `jasper/cli/angle_capture.py` |
| `jasper-measure` | Measure this speaker once, bank the takes, print their ids | measured | `jasper/cli/measure.py` |
| `jasper-crossover-prescriber compose\|status\|packet\|propose\|stage` | Emit one crossover round's evidence packet, read a prescription back through the strict gate, and say where this speaker stands. | advisory (`packet`/`propose`/`compose` save artifacts; `stage` writes pending state; `status` reads) | `jasper/cli/crossover_prescriber.py` |
| `jasper-round open\|wait\|apply\|bank` | Open, wait on, apply and bank a crossover round from the speaker itself. The three wizard verbs scripts/run-crossover-round.py drives from a laptop, over the same transport and the same apply gate, plus the bank that files a finished session in the on-box campaign home. | mutating-with-gates (`open`/`apply`/`bank` write; `wait` does not) | `jasper/cli/round.py` |
| `jasper-round-views entry\|frozen\|repeat\|repeat-floor\|candidates\|agreement\|co-metrics\|directivity\|per-seat\|cloud-binding\|forward-model\|spec-sweep\|gate-sweep\|frequency\|distortion\|classify-features\|findings\|close-reference\|boundary-prior\|delay-landscape\|delay-confirm\|room-ceiling\|room-median\|room-persistence\|bass-fit\|bass-ladder\|inventory` | Read a round's measured evidence. Select standalone views or per-seat --include agreement directivity co-metrics to share a round read. Answers use stdout; details use files. | advisory (analysis views save artifacts; `classify-features` also updates the bundle) | `jasper/cli/round_views/__init__.py` |
| `jasper-null` | Play the summed reverse null and bank one row per coordinate. Measures only; grades nothing. | measured | `jasper/cli/null_door.py` |
| `jasper-audition start\|stop\|status` | Play this speaker at a reduced DSP layer, then put it back | mutating (runtime only; durable graph untouched -- ADR-0193) | `jasper/cli/audition.py` |
| `jasper-declare-geometry set\|show` | Declare measurement rig geometry: speaker/mic heights, distance and optional ceiling, so entanglement_floor_hz has a provenance-labeled, non-measured source on rigs where the measured reflection finder structurally never fires (issue #3502); and optional front/side wall distances, which only the jasper-round-views boundary-prior model reads. | advisory (`set` writes; `show` does not) | `jasper/cli/declare_geometry.py` |
<!-- END GENERATED TOOL MENU -->

Regenerate with `PYTHONPATH=. .venv/bin/python scripts/generate-tuning-tool-menu.py`;
`--check` verifies the committed menu. Tool code owns help, schema, and menu copy.

## Find the analysis that answers the question

| Question | View or record |
|---|---|
| What exists; which details are missing? | `inventory`, prescriber `status` |
| Did the measured tune improve; where did it regress? | `frozen`, `per-seat`, `candidates`; read coverage and measurement scope |
| Level offset or response shape? | `frozen` band `level_deviation_db` and `max_ripple_db` |
| Does it hold off axis? | `directivity`, `agreement`, `co-metrics` over summed poses |
| Does delay/polarity explain the crossover feature? | `delay-landscape`, `jasper-null`, `delay-confirm`; inspect branch levels |
| Does a feature survive gate/pose changes? | `classify-features`, `gate-sweep`, `close-reference` |
| Is a low-end feature what the walls alone predict? | `boundary-prior`; advisory, from declared wall distances |
| Is the distortion window valid? | `distortion`; inspect per-order window and overlap status |
| How stable is the measurement? | `repeat`, `repeat-floor`; distinguish random and systematic error |
| Which part of a prescription did cloud evidence constrain? | `cloud-binding` |
| What is predicted from banked complex solos? | `forward-model`; simulation is not a new capture |
| Show a curve or compare two takes? | `frequency <A> [<B>]` |

The catalog at `/sound/measurements/` includes banked rounds and retained live
sessions. `frequency` can read a banked round, bundle, or take file directly.

## URLs and access

Use the tool's `handoff_url`; it derives from the selected speaker's hostname.
Room browser capture is at `https://<speaker>/sound/room/`; it needs HTTPS and
trust in the speaker's local CA. The crossover surface is
`/sound/speaker/crossover/` and records with the wired Pi microphone. Its state
is separate from the browser's mic indicator; the phone relay is retired.

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
