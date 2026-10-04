# Measurement-loop doctrine

The tuning toolbox lets an LLM choose experiments from measured evidence.
`sudo /opt/jasper/.venv/bin/jasper-crossover-prescriber status` gives the reading order in its
`reading_order` field. No document in it is a fixed campaign sequence.

## 1. The loop

Use the same tools for a baseline, candidate trial, or further comparison.
Each round can be the last. The LLM decides whether the evidence answers the
question or whether another round would help. There is no required round
count, campaign ceiling, plateau stop, or final-confirmation round. Per-take
resource limits and physical protection still apply; the Pi runs bounded
operations, not an autonomous campaign loop.

Measurement is temporary playback. Saving a tune is a separate, explicit act
that names its candidate fingerprint. Banking evidence does not adopt a tune.

### 1a. The layering rule — what a measurement plays through

When tuning layer N, play through that layer and everything below it, with
nothing above it. Enforce this in graph composition for every capture: solo,
summed, cloud, on-axis, and off-axis.

- **Base:** declared routing, topology, crossover, driver protection, trims,
  and applicable delay/polarity.
- **Speaker linearization:** the initial baseline contains only the base.
  Previous linearization, blend, room correction, and preference EQ are absent.
  A candidate adds only the corrective filters, trims, and alignment changes
  being tested. A speaker trial is the one exception: when asked for, it plays
  each candidate whole, with the applied rear, bass and room layers on; the
  default setup applies the speaker with no trial ([ADR-0444](adr/0444-a-speaker-trial-is-optional-and-plays-its-whole-candidate.md)).
- **In-room (room correction and bass extension):** the base plays through the
  applied speaker and rear layers with bass and room off; a candidate adds its
  composed bass and room. The room fit and the bass boost are designed together
  on that one seat set
  ([ADR-0421](adr/0421-bass-has-a-preview-model-the-in-room-preview-adds-the-composed-boost.md)).
  The room program row declares this and the door derives the played graph (see
  [ADR-0429](adr/0429-one-in-room-program-the-room-round-plays-with-bass-and-room-off.md)).
- **Preference EQ:** subjective bass, warmth, and other voicing belongs to
  normal listening. It never participates in linearization measurements.
- **Rear (cardioid) stage:** play the applied speaker layer and the
  candidate's rear section, with bass and room off, and compare rear settings
  on one band against one rear-off reference per position: a candidate with
  its rear muted, else a base that plays no rear stage. The pair take also
  clears the rear section, so it plays the raw woofers. The rear program row
  declares this and the door derives it, and the chosen seat set is the
  in-room base (see
  [ADR-0325](adr/0325-rear-program-compares-measured-symptoms-and-previews-by-superposition.md),
  [ADR-0386](adr/0386-a-rear-pair-take-clears-the-rear-layer-at-the-door.md),
  [ADR-0436](adr/0436-the-cardioid-default-one-pair-take-then-one-seat-trial-against-the-rear-off-base.md)).

Retain household settings while measurement uses its temporary graph. Restore
normal playback after the operation. Record the graph that actually played;
turning off settings by hand is not proof of the layer boundary.

## 2. The authority model

- **Code computes and executes:** numerical analysis, validation, graph
  composition, protected playback, capture, evidence, and cleanup.
- **The LLM judges:** choose the question, read evidence, author candidates,
  combine useful changes, select analyses and programs, and decide when to end.
- **The human handles the room:** place the mic, deliberately start each pose
  batch, report physical changes, and judge the final sound by listening.

Predictions and quality scores inform these decisions. They do not veto a safe,
reversible experiment because its benefit is uncertain or its ideal target is
missed. Missing or stale evidence is disclosed with its limits. It does not
become a permanent ban on trying again.

## 3. The guiding principle — least-bad measured, honed in bites

Choose the least-bad measured configuration for the stated goal. A measured
regression can restore the incumbent; retain the losing candidate and its valid
evidence. Restore ends that adoption attempt, not the ability to learn or run
another round. A mismatch between forecast and measurement is evidence to
interpret, not proof that an otherwise useful tune must be withdrawn.

A losing candidate may contain a useful change. Reuse role, filter, candidate,
and evidence identities to name its parts and source records. Keep expected
effect, measured observation, and interpretation separate. Co-varied changes
do not establish isolated causality. A child assembled from parent parts is
unmeasured until that combination is tested; parent results do not confer
measured or verified status on it.

A sufficient current round can support adoption. Request more on/off-axis,
repeat, or level evidence only when it answers a remaining question. State
coverage limits; do not demand a ceremonial last round.

## 4. The hard-stop enumeration (closed list)

`AGENTS.md` establishes the repository-wide non-negotiables. Their measurement
mechanisms are:

1. **Excitation caps:** declared driver bands, level-duration limits, and the
   session volume admitted by `excitation_safety_plan.py`. The fader hold must
   prove that volume before emission; an unreadable or drifting fader cannot
   establish compliance. The session volume owner sets it; the hold checks it.
2. **Output rails:** limiters, non-positive volume writes, and
   `devices.volume_limit = 0.0`. Composed candidate filters and trims must obey
   the same driver protection and headroom contracts as normal playback.
3. **Declared driver protection:** high-pass floor, required slope, and
   permitted bands. Different base designs use their own derived protection.
   A same-topology candidate batch cannot silently change that premise.
4. **Commissioning SPL stop:** honor the preset's ceiling. Absolute SPL needs
   valid microphone sensitivity and capture-gain context; never invent it.
5. **Firmware hazards:** never call XVF3800 `SAVE_CONFIGURATION`.

The blend door cannot emit boost: its graph stage admits cuts only.
Use a supported driver candidate for a boost experiment (plan ruling R8).
Do not remove an unsupported-candidate refusal until the candidate can be
rendered, protected, and identified correctly.

### 4a. The integrity class — refusing a CLAIM

Invalid input, unavailable instrumentation, corrupt captures, wrong graph or
candidate identity, stale position actions, and unknown restore state are real
tool errors. They cannot be reported as successful measurements. Preserve valid
takes and record failures with their take/pose identity. Report an unavailable
result as unknown, not as an acoustic defect.

After an interruption, say which physical conditions must be re-established.
Do not silently combine captures from incompatible poses, levels, or setup
states. A new placement or explicit recovery/retake needs a new human action;
ordinary consecutive trials at one held pose share one start grant.

Integrity refusal must not discard an already measured tune or permanently
exclude a safe experiment. Fix the input, instrumentation, or capture condition
and reuse the same tools. Per-operation timeouts bound execution; they do not
set a campaign's round count.

## 5. The nanny test

A proposed gate must name the physical harm it prevents or the false result it
would otherwise record. Uncertain benefit, target miss, and a heuristic plateau
are disclosures. Keep them out of experiment authorization.
