# Speaker setup cleanup plan

Proposed 2026-09-21. Planning only; no product changes or deployment.

## Goal

Make a new speaker easy to set up, with one clear next action. Put decisions,
validation, research handling, and DSP calculations in the backend. Keep the
browser responsible for forms, display, navigation, and clipboard access.

Quality determines the boundaries. A smaller diff is not a reason to keep
mixed responsibilities, duplicate rules, or a weak contract. Use the existing
domain owners where they fit; split them where their responsibilities differ.

This plan follows the [JTS3 smoke test](audits/2026-09-21-jts3-commissioning-smoke.md)
and [ADR-0323](adr/0323-speaker-setup-binds-by-driver-computes-on-read.md).
It adds no approval steps, completion tokens, or forced tuning sequence.

## The default experience

1. **Choose the speaker.** Passive or active; for active, two or three amplifier
   channels per cabinet; for three, front/mid/high or front/high/rear cardioid.
   Choose mono or stereo and assign outputs to drivers. Label channel count
   separately from acoustic ways. Generate only layouts the device supports.
   Keep network pairing separate from local output assignment.
2. **Enter driver details.** Model names, relevant installation facts, and notes.
   Default to no resistor pad. Ask for enclosure or horn details where relevant;
   allow unknown optional facts. The front and rear Epique drivers remain separate
   targets even when their model names match. Save details is a normal partial
   save, with no missing-measurement errors.
3. **Get starting values.** Saving details opens the research step with its prompt
   ready. Show one Copy prompt control and one Paste result area. Accept ordinary
   JSON and a JSON code fence. Copying is a convenience, never a prerequisite.
4. **Save to speaker.** Loading the reply saves a draft and shows a short summary:
   output map, crossover, estimated trims, and rear status. Save to speaker applies
   those values through the existing apply path. Full values and edits live under
   Details; opening Details is optional. Expert users can enter values directly.
5. **Tune when wanted.** After confirmed apply, say Base setup active. Show Driver
   linearization as the next recommendation and all other applicable programs.
   Each has a clear purpose and a Copy prompt action. Program pages start with
   their own default measurement plan; extra plans and commands live in Details.

An applied base is usable without measurement. Label starting trims Estimated,
explicit edits Custom, and measured results Measured only when evidence exists.
For the JTS3 base, the rear remains muted until a rear configuration is applied;
show that fact without claiming cardioid behavior has been measured.

Recommend driver → cardioid → bass → room, with room last because earlier tuning
can change its correction. This is guidance, not a lock. All applicable programs
remain accessible. If bass follows room, disclose that room may need another pass.

## Ownership and boundaries

| Concern | Owner | Contract |
| --- | --- | --- |
| Layout and physical output identity | `output_topology.py` and its existing writer | Build and validate topology from layout choices; return exact driver targets and assignments. Do not store a second copy of the layout choices. |
| Saved declarations, research, and explicit edits | `design_draft.py` and its existing atomic writer | One draft file and one writer. Saving a draft does not apply audio. |
| Research text and import | A focused research-import module, existing prompt and binding helpers | Parse, bind by target and model, normalize, and produce a draft update. No DSP or page decisions. |
| Effective driver settings | One pure resolver in the active-speaker domain | Combine declarations, normalized research, and explicit edits once. Every consumer uses this result. |
| Derived protection, trim, and crossover | Existing `driver_safety.py`, `driver_pad.py`, `level_trim.py`, and `crossover_preview.py` | Compute from resolved settings. Preview and apply use the same math and precedence. |
| Setup progress and next action | `commissioning_coordinator.py` | Pure projection of saved inputs, computed readiness, actual applied state, and measurement evidence. No writes or DSP execution. |
| Program definitions and availability | `measurement_programs.py` | One catalog for applicable programs, order, labels, and default plans. Handoff and page views consume it. |
| Running tune | Existing baseline, candidate, and DSP apply owners | Compile, validate, load, confirm, and record through the current apply path. |
| HTTP setup boundary | A focused setup route adapter and application service | Load the needed facts, call domain owners, and return the setup view. No second set of domain rules. |
| Browser | A dedicated speaker-setup entry module and small views | Hold unsaved form text, submit actions, render server results, and copy text. No research merging, trim math, or readiness policy. |

The application service coordinates these owners; it does not absorb them.
Extract measurement-round display and plan-choice work from the setup coordinator
into the measurement UI boundary. Keep preference EQ and general output pages out
of the speaker-setup module. Share existing HTTP, DOM, dialog, and clipboard helpers.

## One source for each fact

Keep the existing draft store, but give its inputs distinct meanings:

- **Declarations:** user-entered model and installation facts, keyed by target.
- **Research:** normalized specifications, recommendations, and their sources.
  Retained reply text is evidence; it is never parsed again as a second authority.
- **Overrides:** only values the user explicitly changes, keyed by target or
  crossover pair. Do not copy every imported value into an override table.

The resolver returns effective settings with field provenance. Explicit edits
override research for editable specifications. Research cannot overwrite a
declared enclosure, pad, output assignment, or other installation fact. Derived
values, including sensitivity trims, are computed unless explicitly pinned.
Reimport replaces research values while preserving explicit edits. A visible
Use researched value action removes an override when the user wants that.

Do not let the preview, compiler, protection code, and UI each choose between
`manual_settings` and `driver_research`. Replace those fallback paths with the
same resolved input contract. Keep full target identity through resolution;
construct role-based crossover inputs only within the correct speaker group.
Front/rear and left/right must never collapse because they share a role or model.

Migrate at the draft read/write boundary. Read existing drafts without writing on
GET. Treat legacy edits without reliable provenance as explicit values, including
unmarked gain trims. Preserve the effective configuration, then write the new
shape on the next explicit save. Do not dual-write old and new fields. Retire the
old interpretation from downstream readers as they adopt the resolver. Existing
applied records and the running tune do not change during this migration.

## Backend contracts

### Setup view

One typed `SpeakerSetupView` is the browser's saved-state contract. It contains:

| Field | Meaning |
| --- | --- |
| `layout` | Saved output assignments and applicable layout choices. |
| `draft` | Editable declarations and overrides, resolved values, and provenance. |
| `base_preview` | Computed crossover, trims, and output summary, when available. |
| `applied` | Saved applied identity and separately reported live load state. |
| `measurement` | Actual captured and applied measurement evidence by program. |
| `next_action` | Action identifier, label, and any current reason it cannot run. |
| `programs` | Applicable program cards, their evidence status, and actions. |
| `issues` | Structured code, target/field, affected operation, and readable text. |

Use one view builder for page load and successful setup mutations. Mutation
responses contain their outcome plus that same fresh view, including programs.
This fixes the current apply/GET response mismatch. The browser replaces saved
state from the response instead of merging partial documents and guessing progress.
Parse failures return field issues and leave the saved state intact. Recoverable
operation failures report the current state where it can be read; a transport
failure stays an error, never guessed success.

The view is computed, not persisted. No saved wizard step, duplicate safety
profile, preview file, or completion flag. Measurement planning and prompt text
load on demand; an ordinary setup GET must not enumerate all measurement plans
or pull heavy analysis into the web process.

### Operations

Retain existing route paths where their meaning fits. Use named operations with
bounded inputs and typed results, not a generic workflow engine:

| Operation | Input and effect |
| --- | --- |
| Save layout | User choices and channel map → validated topology through the existing writer. |
| Save details | Present form fields → saved declarations or explicit overrides. Partial drafts are valid. |
| Get research prompt | Current declaration → prompt text; no state change or request token. |
| Load research result | Reply text → parse, bind, normalize, merge through the resolver, then one atomic draft save. Failed imports change nothing. |
| Get preview | Current resolved inputs → computed base summary; no write. |
| Save to speaker | Current saved draft → existing compile/apply/confirm path. No measurement starts. |
| Get tuning prompt | Program identifier → fresh device-bound prompt from the same catalog and state. |
| Reset / restore | Existing operations remain the owners of their effects; return the fresh setup view. |

The research prompt includes the physical facts the user supplied: loading,
horn, coil wiring, and pad hardware where known. Use explicit fields, not a dump
of settings. Exclude earlier crossover choices, numeric driver protection limits,
and measured correction. Document this narrow extension to ADR-0323 in a new ADR
when implemented. Retain target/model binding; do not restore request hashes,
expected revisions, prompt-copy requirements, or stale-reply approval steps.

Validate at the operation that needs the value. Detail saves validate fields
that are present. Base apply checks its actual driver-protection requirements.
Measurement start checks measurement inputs and the existing SPL stop. Missing
measurement duration or bands must not produce setup errors or block base apply.
Unknown optional installation facts remain unknown, not invented values or gates.

Only confirmed live load produces an apply-success result. Failure uses the
existing rollback behavior and reports what remains active. Draft edits do not
silently retune or revoke an accepted tune. Preserve existing checks for actual
unsafe output changes. Keep the 0.0 dB ceiling, driver caps, and SPL stop intact;
do not add safety policy outside the project's defined non-negotiables.

## Delivery sequence

Each slice ships with behavior tests at its boundary. Do not postpone correctness
until a final cleanup pass. Split by concern, not an arbitrary line count.

1. **Repair the backend state contract.** Introduce the setup view and make base
   apply the next action once its inputs are ready. Separate base-applied,
   measurement-captured, and correction-applied status. Return the same view after
   apply and reload. Make the existing page consume it first. Pass: no false
   experiment badge, no empty program menu, and partial saves guide to research.
2. **Consolidate settings and research.** Add the draft resolver and compatibility
   reader; move parsing, merge rules, candidate selection, and sensitivity-trim
   policy out of JavaScript. Route preview and apply through that same resolver.
   Add physical facts to the prompt. Delete superseded client and backend paths.
   Pass: old effective values survive; explicit edits survive reimport; target
   bindings stay correct; preview and applied base agree.
3. **Replace the setup page around the five-step flow.** Move layout construction
   to the topology owner. Extract a dedicated speaker page from the mixed sound
   module. Default to the short form and short base summary; retain full editing
   under Details using the same data and operations. Pass: reset → layout →
   details → copy → paste → save works without opening advanced controls.
4. **Complete the tuning handoff.** Consolidate program metadata and applicability;
   show all relevant programs immediately after apply. Use fresh identity-based
   URLs and purpose-specific defaults. Move command and plan details out of the
   default path. Pass: JTS3 prompts target JTS3, no irrelevant program choices,
   and no completion claim without the corresponding evidence.
5. **Verify the full flow on JTS3.** Back up, reset, and repeat the user path from
   blank state through confirmed base apply and the linearization entry. Reload
   at saved boundaries. Check clipboard behavior in the actual browser. Verify
   the loaded output graph, then restore the original tune and settings and
   compare them with the backup. Do not run tuning sweeps as part of this test.

## Proof of quality

- Domain tests cover mono, stereo, two-way, true three-way, and rear cardioid;
  repeated models remain distinct targets. Unsupported hardware layouts get a
  direct explanation from the topology owner.
- Resolver tests cover no pad, known pad, unknown optional facts, explicit trims,
  derived trims, reimport, wrong target/model, and legacy preservation. Reuse the
  existing trim math rather than porting a second formula from JavaScript.
- Service tests cover valid partial saves, atomic failed imports, matching
  preview/apply inputs, failed apply recovery, and identical post-save/reload
  state. Assert structured behavior, not prose or source text.
- Browser tests exercise the whole default path, direct expert edits, keyboard
  use, clear field errors, and repeat apply after a recoverable failure. Capture
  the real initial screen and tuning handoff to check visual clarity.
- Run the relevant tests per slice, `scripts/test-fast` before push, and
  `scripts/test-merge` before merge. Use the required code review; add adversarial
  review where changes touch DSP math or the non-negotiable protection paths.
  Deploy only through `scripts/deploy-to-pi.sh` and verify the device surface.

Done means a fresh speaker reaches a confirmed active base through the obvious
path; advanced edits remain available; tuning is optional and easy to find; and
the frontend contains no second implementation of speaker policy. The live test
proves configuration and load behavior, not sound quality without measurement.
