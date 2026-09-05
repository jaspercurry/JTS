# ADR-0228: Rulings carried out of REFACTOR-TUNING-2026-08 on its retirement

- **Date:** 2026-09-03
- **Status:** Accepted. Entry 9 (row 4g) is superseded by
  [ADR-0230](0230-the-summed-graph-commissioning-lane-is-deleted.md).

## Context

`docs/REFACTOR-TUNING-2026-08.md` (1,868 lines, waves 0–8) was the tuning-engine
refactor's planning authority. Its waves have landed or been superseded, and
what is left is owner rulings quoted inside it — several with no other home,
some cited by name from code and test docstrings. The plan is retired in the PR
that records this ADR. [ADR-0227](0227-owner-rulings-the-prose-pass-surfaced.md)
is the same register for the rulings the prose pass surfaced.

## Decision

**These rulings stand, and this file is their home until one earns its own ADR**
(which then supersedes the row). Each row names its section in the retired plan
and quotes the ruling sentence.

### 1. S1 — the four verbs are the vocabulary (§4 S1, owner 2026-08-25)
> "Measuring is measuring" — baseline, re-measure and candidate-check are one
> parameterized verb; "propose and prescribe and recommend are the same thing";
> the plain word wins.

One vocabulary, no second layer; VERIFY is a region of code, not a verb.
ADR-0198 deleted the engine's unwired `analyze`/`recommend`/`save` half — the
words survive as the loop's language (the doctrine §1, `MEASURE_KIND_*`).

### 2. S3 — bank the information, phase included (§4 S3, owner 2026-08-25)
> "When we take a measurement, it should just be easy to get all the information
> we need… right now, let's just save the information."

`DriverResponse` persists magnitude **and** phase, so a banked session is
re-analyzable offline forever. Measurement management is explicitly future
scope.

### 3. S4 — the bar is end-state deletion (§4 S4, owner 2026-08-25)
> "We're not investing in systems we're going to be deleting."

Temporary adds inside a wave are fine; throwaway scaffolding is not built at
all. Cited by `tests/engine_twin.py`, whose licence to exist is this rule.

### 4. S5 — the old path dies immediately (§4 S5, owner 2026-08-25)
> "The old path should die right after we've got the new one in… fallbacks
> aren't a thing. We're not going to have duplication… deleting old systems
> whole hog."

Build new → prove → delete old, in one change. A route kept past its proof is a
proof bracket for that proof only: no fallback flags, no coexistence windows.

### 5. S7 — the unit of deletion is the test FUNCTION (§4 S7, 2026-08-25)
> The unit of deletion is the TEST FUNCTION, not the file.

Default-aggressive is licensed for dying happy-path choreography only; three
exceptions go to the rewrite queue — failure-branch pins, cross-process and
cross-language pins, and deadness-enforcing tests, which die in the same PR as
their subject, never before (the mechanism ADR-0018 leans on).

### 6. S8 — the level fact is output through the handover region (§4 S8, 2026-08-25)
> "Level-matched" means matched ACOUSTIC OUTPUT THROUGH THE HANDOVER REGION.

After the target filters, the two driver traces are equal at Fc and each sits
−6 dB against the summed target. Passband-average sensitivity is **not** the
level fact: it is the starting estimate, kept and its delta disclosed, never
silently reconciled. Cited by `driver_acoustics.py`.

### 7. S12 — a mic-only capability hole is a loud stub (§4 S12, owner 2026-08-25)
> "If there's a hole in our existing capability that the plan points out… for
> just microphone measurements… we would just stub it for the time being."

The `measure`/`analyze` parameter surface ships complete; an unimplemented
regime returns a named not-implemented disclosure, never a silent skip, and the
API shape does not change when the capability lands. New-hardware regimes stay
out — ADR-0200 rules that half. Cited by `crossover_v2/contracts.py` and
`measure_spec.py`.

### 8. MS-17 — mover-agnosticism (§2 MS-17, owner ruling 2026-08-25)
> "Our cleaned up, tidied code is at its core AGNOSTIC of whether a robot arm
> took the measurement or a human took the measurement."

The engine below the front-end seam holds zero arm-specific and zero
wizard-specific code; the mover may appear in provenance and never in an `if`.
Pinned by `tests/test_measurement_mover_agnostic.py`.

### 9. Row 4g — the commissioning lane is repaired, not abandoned (§3 wave 4g)

**Standing; PR #3836 awaits the owner's re-ruling.**

> The commissioning lane is being REPAIRED, not abandoned — the owner settled
> #2202 as *fix*, so this is now the producer path, not the deletion path.

> The free −2,089-line deletion this plan previously booked stays withdrawn.

Both premises this row rests on are recorded as no longer holding at HEAD:
[ADR-0188](0188-wired-first-measurement-relay-parked.md) parked the relay lane
the producer build needed, and
[ADR-0197](0197-the-commissioning-capture-stack-is-deleted.md) applied that
reasoning to the sibling module. The ruling is carried unchanged
regardless — re-deciding it is the owner's, not an agent's.

### 10. R4 — jts3 is a TEST BOX (§6 R4, owner ruling 2026-08-25)
> "Who cares if it wipes out a tournament winner? We don't care about the
> tournament winner. In general, we should apply stuff to jts3 when we have
> something we need to test."

This overruled the inherited warning never to apply an unmeasured candidate to
jts3: that is a quality unknown, never a safety one — the per-driver protections
are structural (MS-13).

### 11. Archival declined for two plans (§4 ratifications, owner 2026-08-26)
> `gating-v2-plan.md` and `room-correction-regime-plan.md`: **No — both stay
> STANDING.**

### 12. §0 rule 1 and §6 R7 — extract the rulings before the code moves

> Extract the rulings before the code moves.

> Prose deletion can delete the only record of a ruling.

Eleven ADRs (0006, 0008–0010, 0012–0017, 0019) cite `§0 rule 1, §6 R7` as the
authority for their own existence, and `REFACTOR-CUTOVER-2026-08.md` §6.1 cites
R7 beside S7. Those citations are append-only and stay as written; this row is
what they resolve to.

## Already recorded — do not re-extract

- **S2** (*"2202 does seem like it should be fixed"*) is row 4g's premise and
  travels with entry 9.
- **S6** (the duck dissolves), **S9** (the deviation table is deleted) and
  **"drop the attribution, do not grow the charter"** — all executed.
- **S10** (attestation never gates operation) → AGENTS.md's Guards default,
  [ADR-0101](0101-proven-once-disclose-on-change.md), doctrine §4a.
- **S11** (hardware licence during the refactor) → superseded by
  [ADR-0192](0192-the-campaign-is-the-validation.md), which also holds roster
  rows R-1/R-3/R-4 and acceptance rows 9/10; acts 4 and 6 defer per ADR-0188.
- **§6.2 `RecordStore` is THE durable-write seam (FOLD)** → ADR-0227 entry 12.
- **#1738, wire or delete `jasper/bass_extension/`** →
  [ADR-0018](0018-bass-extension-stays-parked.md) (parked, a third answer).
- **The R-6 impedance jig** →
  [ADR-0200](0200-the-measurement-toolbox-is-microphone-only.md).
- **R-2** (wider horizontal orbit, DI consumption), an open plan row rather than
  a ruling → [#3865](https://github.com/jaspercurry/JTS/issues/3865).

## Consequences

- The plan file goes without taking twelve owner rulings with it. Live code and
  docs that cited it by section now cite a row here; the ADRs that cite it stay
  as written (append-only), and resolve to entry 12.
- Twelve decisions in one file breaks this directory's one-decision-per-file
  rule, taken for ADR-0227's reason: each row is a quoted sentence plus its
  scope. A row that grows an argument earns its own ADR, superseding the row.
- Entry 9 is the only row that does not describe HEAD, carried standing with its
  premises disclosed: retiring the document that held a ruling is not a
  re-ruling.
- Rejected: archiving the plan to `docs/historical/`. ADR-0199 closed that move
  off — an archived doc still greps and still gets trusted over the code.
