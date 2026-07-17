# Correction/crossover UX rework — Wave-3 Codex delegation kit

This directory holds one self-contained implementation prompt per
workstream of Wave 3 of the correction/crossover finish-line campaign
— the information-architecture rework of `/correction/`'s room and
active-crossover flows. The architecture plan of record is Jasper's
own campaign planning notes (`campaign/VALIDATION-FIX-PLAN.md`, Wave 3
section, plus `campaign/punchlist.md`'s hardware-found evidence); these
prompts are the execution contracts derived from Jasper's own resolved
IA decisions (recorded in the campaign notes' "SCOPE CHANGE" entry —
Wave 3 was deliberately deferred from the autonomous overnight run to
be implemented later via Codex).

**A note on `campaign/` citations throughout this kit**: they are
**not part of this git repository** — they live in Jasper's private
planning workspace and will not exist in a Codex checkout. Every
`campaign/`-cited fact that a workstream actually needs to execute has
been independently re-verified against the current codebase and
restated directly in that workstream's own prompt (Context, Preflight
facts, Requirements). Treat every `campaign/` reference below and in
each prompt file as **provenance** ("this is where the finding came
from, for a human with access to that workspace to cross-check"), never
as a required-reading step — do not attempt to open those paths.

**Operator usage:** start a fresh Codex session per workstream and
say "Read `docs/correction-ux-wave3/w3.N-….md` and execute it." Do
not hand Codex more than one workstream at a time. Workstreams must
land in the order below except where a prompt says otherwise.

| Workstream | Prompt file | Depends on |
|---|---|---|
| W3.1 Single entry + active/passive declaration | `w3.1-declaration-and-gating.md` | none |
| W3.2 One crossover surface | `w3.2-one-crossover-surface.md` | W3.1 merged |
| W3.3 Stepper + instruction placement | `w3.3-stepper-and-instructions.md` | W3.2 merged |
| W3.4 Room flow parity | `w3.4-room-flow-parity.md` | W3.3 merged |
| W3.5 Jargon + copy sweep | `w3.5-jargon-and-copy-sweep.md` | W3.3 merged (touches the same files W3.3 rewrites) |
| W3.6 Conventions + docs | `w3.6-conventions-and-docs.md` | none — safe to run any time, including in parallel with everything else |
| W3.7 Region-scoped quality gates (design task) | `w3.7-region-scoped-gating-design.md` | none for the investigation; run last so its design note reflects the post-W3.1–3.5 code shape |

Every workstream prompt **incorporates this README by reference**.
Codex: read this file completely before your workstream file.

---

## State of the world (read before starting anything)

Wave 2 of the campaign closed on `main` before this kit was written.
Do not re-litigate or re-derive any of the following — it is settled,
merged, hardware-touched behavior that Wave 3 builds on top of
without changing:

- **Closed-loop measurement level solver** (#1543, follow-up sign/escalation
  fixes #1552, #1555, #1561) — a single-owner solver picks the
  quietest `(main_volume_db, commissioning_gain_db)` pair that clears
  the worst-band SNR floor, jointly, replacing the old "restart the
  level check and hope" loop. `bounded_low` as a user-facing nudge and
  the manual "restart level check" retry loop are **deleted** — do not
  reintroduce either vocabulary or affordance in Wave-3 copy work.
- **Truthful per-band SNR** (#1563) — the deconvolution-artifact bug
  that overstated sub-bass noise by 40–50 dB (punchlist #51, "the
  campaign's biggest finding") is fixed. Pre-#1563 SNR numbers in old
  punchlist entries (11–16 dB) were measurement artifacts, not room
  reality; current hardware runs read true SNR in the 60+ dB range at
  normal desk placement. This does not change Wave 3's scope — it
  explains why some of the older punchlist entries describe walls that
  no longer reproduce.
- **v3 session-spanning relay capture** (#1550 dormant wiring, #1560
  capture-page + ambient stats + one-tap mic, #1562 Pi-side flip) — one
  relay session now drives an entire repeat set instead of one QR
  round-trip per repeat. **Known open hardware bug, NOT in Wave-3
  scope:** punchlist #52 (run 21) found a reserve-then-guard ordering
  conflict between the v3 capture-plan reservation and an older
  server-owned-next-step guard (`correction_setup.py:5359` and
  `:4743`) that can 3-for-3 block the very first driver capture on
  hardware. As of this kit's writing that fix has **not** landed on
  `main`. If a Wave-3 hardware validation pass hits it, that is a
  pre-existing Wave-2 defect — name it in the pilot report and move on;
  do not attempt to fix it inside a Wave-3 PR (it is outside every
  workstream's file allowlist).
- **Durable household mic identity** (#1540 Pi-side record, #1564
  one-tap stored-calibration resolve-by-id) — `/var/lib/jasper/correction/
  household_mic.json`, owned by `jasper/correction/household_mic.py`,
  now remembers the mic+calibration that last succeeded and pre-fills
  it with a one-tap confirm on both room and crossover flows. This is
  the exemplar module W3.1 mirrors for the new crossover-declaration
  record (see that prompt).

If you find yourself about to touch the level solver, the SNR
estimator, the v3 relay protocol, or the household-mic record's
schema, stop — that is not Wave 3. Wave 3 is presentation, navigation,
and copy layered on top of settled Wave-2 behavior, plus one scoped
design investigation (W3.7).

## Evidence sources

- `campaign/VALIDATION-FIX-PLAN.md` (not in this repository — see the
  note above) — Wave 3 section, the UX acceptance rubric, and the
  from-scratch E2E milestone, both reproduced in full below since this
  kit is meant to be self-contained without access to that file.
- `campaign/punchlist.md` (not in this repository) items 31–52 —
  hardware-found evidence, cited by number in each workstream prompt
  as provenance; the substance of every cited item is restated inline
  in the relevant prompt. Punchlist items 34–46 are the direct
  UX-architecture source material; #51 and #1563 explain why the older
  SNR-wall entries no longer reproduce; #52 is the known-open item
  above.
- [`docs/active-crossover-information-design.md`](../active-crossover-information-design.md)
  — product/architecture design of record for the crossover builder,
  including the Language guide (§"Language guide") that W3.5's copy
  sweep must match.
- `AGENTS.md` — "Config ownership" (which pattern owns the declared
  crossover fact), "Web wizard conventions" and "Canonical design
  system" (binding for every page touched), "COAH quality bar".

## The engineering charter (binding for every workstream)

You are implementing a specified slice of a reviewed information
architecture. The review bar is a staff maintainer who values small,
boring, correct diffs. These rules are hard constraints, not
suggestions:

1. **The file allowlist is absolute.** Each workstream prompt lists
   the files you may create or modify. If completing the spec seems
   to require touching any other file, STOP and report (see protocol
   below). Do not "just quickly" edit a neighbor.
2. **Smallest diff that satisfies the spec.** When two designs both
   satisfy the spec, pick the one with less code and note the
   rejected alternative in one sentence in the PR description.
3. **No new abstractions beyond those named in the prompt.** No base
   classes, factories, registries, or "manager" objects the prompt
   does not name. Reuse the shared wizard primitives named in
   AGENTS.md "Web wizard conventions" (`canonical_page`, `json_island`,
   `toggle_html`, `jtsConfirm`/`jtsAlert`, `escapeHtml`, `h()`/`svg()`,
   the shared dialog/escape/dom modules) — do not re-implement any of
   them.
4. **No speculation.** Nothing for a future workstream, no
   configuration knobs the spec doesn't name, no `TODO` scaffolding.
   If you catch yourself writing "this will be useful later," delete
   it.
5. **Do not refactor, rename, reformat, or "improve" existing code**
   outside what the spec asks for. Match the local style of the files
   you touch, even where you'd do it differently.
6. **Reuse before writing.** Each prompt names the existing helpers,
   modules, or exemplar patterns to build on (e.g. W3.1's declaration
   record mirrors `jasper/correction/household_mic.py` exactly). If
   you find yourself re-implementing something, stop and look again.
7. **Ambiguity protocol.** If the spec is ambiguous on a non-safety
   point, choose the *simpler* interpretation and record the choice in
   the PR description. If the ambiguity touches audio safety
   (levels, filters, protection, emission) or the safety-authority
   boundary described below, STOP and report instead.
8. **Tests are part of the deliverable, and they are pinned in the
   prompt.** Write exactly the listed test coverage (more cases are
   welcome; fewer is a failure). Deterministic, hardware-free, no
   network, no sleeps.
9. **Every value must trace to the prompt, the plan, or existing
   code.** Do not invent thresholds, copy, defaults, or magic numbers
   not named in the prompt or the design doc's Language guide.

### The safety floor is untouchable, in every workstream

None of Wave 3 touches measurement math, DSP, or admission gates
(W3.7 investigates and designs but does not implement a gate change
without a separate sign-off — see that prompt). Regardless, every
workstream must preserve, unmodified:

- `jasper/active_speaker/graph_safety.py`, `driver_safety.py`,
  `excitation_safety_plan.py` — the admission/excitation ceilings.
- `CamillaDSP` `devices.volume_limit == 0.0` and
  `CamillaController.set_volume_db`'s positive-gain clamp.
- The `ROOM_AUTHORITY_*` derivation in
  `jasper/active_speaker/setup_status.py` — this is the safety
  authority that decides whether Room correction may actually run. A
  UX-layer declaration (W3.1) is gating for navigation only; it must
  never become an input to this derivation. "Measure truth over
  attestation" — the same principle that drove the AGC slope
  verification (punchlist #1531) and the household-mic
  device-mismatch backstop — applies to every declaration this wave
  adds.
- Volume restore paths in `jasper/audio_measurement/ramp.py` and
  `jasper/correction/level_match.py`.

### Single source of truth

- The active-speaker **design draft**
  (`/var/lib/jasper/active_speaker_design_draft.json`, read/written via
  `jasper/active_speaker/design_draft.py`) remains the one writer for
  driver layout, LLM research/import, and manual crossover settings.
  W3.2 moves *where* the draft is edited (page surface), never *what*
  writes it or *where* it is stored.
- The crossover envelope
  (`jasper/active_speaker/crossover_envelope.py`'s
  `build_crossover_envelope`) remains the single JSON screen contract
  for the crossover flow; screen/next_action continue to derive from
  durable state, never from client-side memory.

## Preflight protocol (run before writing any code)

1. `git fetch origin main` and branch from `origin/main`. Record the
   base SHA; it goes in your PR description.
2. Verify every item in the workstream prompt's **Preflight facts**
   list (files exist, functions/line ranges are what the prompt
   claims). One command per fact is fine (`git grep`, `sed -n`).
3. If any fact fails: **do not adapt silently.** Stop and report
   which fact drifted — `main` moves fast (see AGENTS.md "PR workflow
   on a fast-moving main"), and drift means the prompt needs a
   revision, not that you should improvise around it.
4. Read the workstream's **Required reading** in the order listed.

## Process rules

- Branch name: `codex/correction-ux-w3.N-<slug>`.
- Run `scripts/test-fast` before every push; run the workstream's
  **Acceptance commands** before opening the PR.
- One PR per workstream unless its prompt splits it (W3.2 explicitly
  stages into multiple PRs — see that prompt).
- PR description must contain: base SHA, the workstream file name,
  the acceptance-command output summary, any ambiguity choices made
  (charter rule 7), and any allowlist near-misses.
- PR bodies end with the repo's standard agent attribution.
- Never merge with red CI; never push to `main`; never touch
  `.github/`.

## The adversarial review gate (mandatory, every workstream PR)

After your acceptance commands pass and before merge, the branch must
survive the **independent adversarial review** defined in
[`adversarial-review.md`](adversarial-review.md) — the canonical JTS
staff-maintainer review prompt (COAH bar: separation of concerns,
single source of truth, safety, observability, resilience, tests,
docs), extended with two lenses that matter specifically for this
program:

- **SSOT check**: does the change introduce a second writer for the
  design draft, the crossover envelope, or the new crossover
  declaration record? Any second writer is a Blocker.
- **Separation-of-concerns check**: does UX-layer gating (the
  declaration, the stepper, screen routing) stay strictly downstream
  of the safety-authority derivation (`ROOM_AUTHORITY_*`,
  `setup_status.py`), never upstream of it? A declaration flag that
  the safety authority reads as evidence, instead of measuring the
  topology itself, is a Blocker.
- **JTS checklist** from `adversarial-review.md` in full — audio
  safety, hardware topology, observability, resilience, performance,
  web/security (CSRF ordering, escaped untrusted strings, no
  `innerHTML` string concatenation), secrets/config, tests, docs.

The loop is specified in `adversarial-review.md`: fresh reviewing
context (never the implementing session), fix every Blocker and
Should-fix, re-run until a clean pass (**zero blockers, zero
should-fixes**), paste the final verdict into the PR. A workstream PR
without a clean review verdict does not merge — treat a missing or
failed gate the same as red CI. Green CI (the required `pytest`,
`rust`, `shell`, `js` checks) plus the clean review verdict together
authorize a **squash-merge**. Do not merge without both.

## Stop-and-report protocol

A good blocked-report is short and specific: the preflight fact or
spec line that failed, what you found instead (file/function/line),
and one suggested resolution. Post it as the PR description of a
draft PR (or as your final message if no branch exists yet), then
stop. Do not partially implement around a blocker.

---

## The UX acceptance rubric (applies to every screen, every workstream)

Verbatim from `campaign/VALIDATION-FIX-PLAN.md` — every hardware
validation pass in every workstream prompt below applies this rubric
per screen and transition:

1. Does this make sense to someone who knows nothing about the
   internals?
2. Is there exactly one clear next action?
3. Is the copy concise?
4. Does it state the VALUE / what will happen, not technical jargon?
5. Does the user know they need to do it (discoverability)?
6. Visual basics: appropriate padding/margins between buttons and
   elements (screenshot-based judgment, not just DOM reads).
7. Spoon-feed one step at a time — instructions only right before the
   action they describe.
8. If something stops the user, do they know WHY and have a way to
   fix it?

Report findings as **text**, per screen, with screenshots noted (not
attached inline) — "looks bad" is a valid finding, capture it. This
mirrors the amended overnight-run rule
(`campaign/OVERNIGHT-2026-07-16-NIGHT2.md` item 3): pilots apply the
rubric including visual judgment; they do not paste screenshots into
a review context.

## Program exit validation: the from-scratch E2E milestone

Run once, after W3.1–W3.5 are merged and hardware-validated (W3.6 and
W3.7 do not gate it — W3.6 is docs/conventions only, W3.7 is a design
note). This is the program's acceptance test, not a per-workstream
one:

Authorized (Jasper, 2026-07-16): manually remove the applied
crossover on JTS3 and reset commissioning state **using the new
flow's own controls** — resettability is itself part of what is under
test; a gap here becomes a punchlist item, not a workaround. Then a
pilot runs the never-commissioned path end to end as a fresh user,
applying the UX rubric above at every screen:

**declaration → driver setup with the real hardware (Dayton Epique
E150HE-44 5.5" woofer + B&C DE250 1" compression driver — the LLM
research/import step gets genuinely filled out, not stubbed) → mic +
level → driver measurements → alignment → apply → verify.**

Chrome pilot + UMIK-2 standing authorization already confirmed for
this program; hardware passes are encouraged throughout Wave 3, not
just at this final milestone. If the run-21 v3 reserve-guard bug
(punchlist #52, see "State of the world" above) blocks the first
driver capture, that is expected and pre-existing — note it, use the
v2 fallback path if the pilot's tooling supports one, or pause the
milestone and report rather than fixing it inline.

## Sequencing rationale

- **W3.1 first**: it creates the declaration record and the
  `/correction/` entry screen every later workstream's navigation
  depends on.
- **W3.2 second**: it moves the crossover setup surface into
  `/correction/crossover/`, which W3.3's stepper rework and W3.5's
  copy sweep both need to land in its final location, not twice.
- **W3.3 third**: the stepper/instruction-placement rework touches
  the same envelope and page-shell code W3.2 just relocated.
- **W3.4 and W3.5 after W3.3**: W3.4 applies W3.1–W3.3's standards to
  the room flow, and W3.5's copy sweep should not fight W3.3's
  structural rewrite of the same files.
- **W3.6 any time**: pure docs/conventions-test tightening, no
  product surface dependency. Good filler between the sequential
  items above.
- **W3.7 last (or in parallel, as a design track)**: it is a design
  investigation, not a code change gated on Wave 3's UX surfaces
  landing first — but running it last means its findings section
  reflects the actual post-Wave-3 code shape rather than a moving
  target, and it explicitly is not blocked on anything above.

After all of W3.1–W3.6 merge and the from-scratch E2E milestone
passes, update `campaign/punchlist.md` and the memory hub entry
`project_correction_crossover_finish_line_2026_07` the same way each
Wave-1/Wave-2 PR did — that housekeeping is Jasper/Fable's, not a
Codex deliverable inside these prompts.
