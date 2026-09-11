# Measurement-loop doctrine

The tuning toolbox lets an LLM choose experiments from measured evidence.
The [runbook entry contract](tuning-operator-runbook.md#entry-contract) is the
short starting point. The [methodology](tuning-methodology.md) is optional
science guidance. Neither is a fixed campaign sequence.

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
  being tested.
- **Room correction:** hold the accepted speaker tune below it. Preference EQ
  remains absent.
- **Bass extension:** test fixed-level windows inside one run. Disclose the Main,
  Aux1, and stimulus-level table for each row
  ([ADR-0304](adr/0304-the-bass-level-axis-is-fixed-level-windows.md)).
- **Preference EQ:** subjective bass, warmth, and other voicing belongs to
  normal listening. It never participates in linearization measurements.

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

[AGENTS.md](../AGENTS.md#non-negotiables-closed-list--nothing-else-is-safety)
owns the repository-wide non-negotiables. Their measurement mechanisms are:

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

The blend door cannot emit boost: its graph stage has no boost headroom term.
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
