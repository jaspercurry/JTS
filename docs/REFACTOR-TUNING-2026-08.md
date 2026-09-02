# 12 — The tuning-engine refactor plan (FINAL — all gates settled 2026-08-25)

> **Location note (2026-08-25):** this plan's evidence base — inventory
> fragments `00`–`11` cited throughout — lives in
> `captures/tuning-stack-inventory-2026-08/` on the bench machine
> (gitignored bench evidence). The numbers those citations back are stated
> inline here.


**Status: final.** Every gate the inventory escalated is closed — twelve owner
rulings, S1–S12, recorded in §4 — and both programs have reconciled their
boundaries in writing (§6 R5). One decision arrived *with* newly-assigned scope
rather than being left open: **#1738**, wire or delete `jasper/bass_extension/`
(§4). A fresh session executes this. **Read §6's R9 and R5 first**: the repo's
governance surface changed after the gates closed, and the charter — not
AGENTS.md's retired right-sizing directive — is what this plan's review gates now
cite.

**Evidence base.** This plan decides. It does not survey. Every number below
cites a fragment of the tuning-stack inventory (`00`–`11` in this directory),
`docs/DEEP-AUDIT-2026-08-25.md`, or a named capture campaign. Nothing here was
re-derived. Where the evidence was not enough to plan, §7 says so instead of
guessing.

**Provenance.** The inventory pinned itself at merged `main` `e064fa43d`. This
plan was written at the same commit — `git rev-parse HEAD`, `origin/main`, and
`00`'s pin all read `e064fa43d`, and the god-file counts reproduce byte-exactly
(`13,459 + 9,563 = 23,022`). Plan and evidence share one tree.

---

## 0. Why we are doing this

### The north star

The owner, 2026-08-25:

> "have a speaker play the test tones at a volume that we deem as correct using
> the microphone, and then take a bunch of measurements in a bunch of different
> locations, and then run a bunch of analysis on them, and then allow an LLM to
> access all of that, and then create prescriptions, and then be able to test a
> variety of configs at each physical measurement point… Everything should be
> modular so that if the web interface is being used to take the measurements,
> great. Or if the LLM is driving a robotic arm, that's also great. But they
> should all use the same plumbing."

The bar:

> "any third party can walk in and understand exactly how our audio-tuning
> pipeline is organized."

The charter, 2026-08-24 (quoted verbatim in `00 §The charter this serves`):

> "take the good ideas. the contracts. the fundamental actions. and wash away
> the cruft. no nannys. one volume owner and source of truth. keep all of the
> analysis functionality. just simplify and wash away the cruft."

*(The north-star and bar quotes are the owner's spoken direction on 2026-08-25,
recorded here for the first time. The charter is already in the tree at
`00:16–19`.)*

### Four rules the owner set

**1. Net lines go DOWN at the END STATE, and we count them.** SETTLED,
2026-08-25: **end-state deletion is the bar.** Temporary adds during a wave are
fine. The hard rule is *"we're not investing in systems we're going to be
deleting"* — **no throwaway scaffolding.** Anything built is built to keep, or it
is not built. *Caveat that still travels* (`00 §5.4`, `00 §R5`): 60% of the god
files' 23,022 lines are prose, and several rulings exist **only** as docstrings
there. Success is measured in seams and call-graph depth; the line count is the
budget, not the goal. **Extract the rulings before the code moves.**

**2. Configs go inside positions.** Moving the mic is expensive — a person
walks, or an arm drives. Patching a config is cheap. So the loop is:

```
for position in positions:          # EXPENSIVE — a mic move
    for candidate in configs:       # CHEAP — a patch, no graph swap
        run(stimulus) -> capture -> bank
```

Not new policy. The ratified principle this rule descends from is already in the
doctrine — *"every mic movement gathers the maximum information it can support,
not the minimum that answers one question"*
(`docs/measurement-loop-doctrine.md §1`, quoted as **current** doctrine; that
file's six-step loop is rewritten to the settled four verbs in wave 7). Today the
code inverts the rule: the inner loop costs 2 config swaps, 2 ducks and ≥0.9 s of
duck ramp per stimulus (`03 §Moving parts, counted`). Fixing the inner loop is
the single change that makes the north star's *"test a variety of configs at each
physical measurement point"* affordable.

**3. The old path dies immediately.** SETTLED and strengthened, 2026-08-25:
*"the old path should die right after we've got the new one in… fallbacks aren't
a thing. We're not going to have duplication… deleting old systems whole hog."*
Every wave runs **build new → prove → delete old**, in that order, and the
deletion is not deferred to a later wave. **No fallback flags. No coexistence
windows** beyond the proof step itself. This changes what "rollback" means
everywhere in §3: rollback is `git revert` of the wave's PRs, never a runtime
path kept alive as insurance.

**4. AEC is out of scope.** See §1's fence line.

### Non-goals — say them out loud

- **AEC is explicitly OUT OF SCOPE** — not the bridge, engines, profiles, or
  reconciler. **Fence line only: the same output device contract** (§1, MS-7).
- **Measurement management is future work, not this plan.** Session structure,
  browsing, and deletion of stored measurements are explicitly deferred by the
  owner. This plan makes the information easy to bank and easy to read; it does
  not build the library around it.
- **The voice side is not touched.** No wake, providers, prompt, or cues.
- **The audit program's zone is not touched.** `11` draws the boundary; the two
  programs meet by handoff, never by shared files.
- **The truth layer's behaviour does not change.** It ports whole and unedited
  (`00 §4.1`): every `*.py` under `jasper/audio_measurement` and
  `jasper/active_speaker/crossover_v2`, holding the gated analysis units that
  group the **20 produced `ProgramAnalysis` fields**
  `tests/test_program_analysis_field_census.py` counts from source. They group
  into **15 units**, one per distinct gate; W2-a's table is where those fifteen
  are written down, and it carries their pin. Membership is a rule about two
  package roots rather than a number to re-count, and
  `tests/test_correction_boundary_ssot.py` pins the direction that makes it a
  layer at all — neither package imports its own front end. That is a stated
  method, which the draft's unenumerated "92 units" never had;
  `REFACTOR-CUTOVER-2026-08.md` §2 records the failed re-derivation. The two
  known defects are in what the caller *hands* them, and they get fixed as part
  of defining the record contract — never as pre-work (`00 §3.3`).
- **The apply/rollback transaction is never a refactor target.** It is the one
  irreversible act and the only path that writes a live DSP graph. Moving it
  buys nothing and risks the thing the household hears.
- **We do not re-extract analyze and prescribe.** They already escaped: 9,526
  package lines (28%) are consumed only by `jasper/cli/*` and `scripts/*`, and
  four of those five entry points import **zero** from either god file
  (`06 §Judgment 2`).

---

## 1. The target architecture

One engine. Two thin front ends. One truth layer, untouched, behind a record
contract.

```
 FRONT ENDS — thin. They pick WHAT to measure and WHERE. Never HOW.
   ┌─ /correction/ web wizard ─┐        ┌─ LLM + robotic-arm runner ─┐
   │  a person moves the mic   │        │    the arm moves the mic   │
   └────────────┬──────────────┘        └──────────────┬─────────────┘
                └──────── THE SAME VERBS ──────────────┘
                                 ▼
 THE ENGINE — one session object
   VOLUME OWNER         SESSION GRAPH          CAPTURE RECORD
   one write door       installed ONCE         one shape
   4 claim kinds        role-routed            place is a field
   ranked claims        crossover-free         DriverResponse banked
   one tolerance        tweeter-protected      provenance + honesty
         │                    │                        ▲
         │        patch_config per candidate (cheap)   │ write
         ▼                    ▼                        │
   VERBS   measure · analyze · recommend · save
           ONE cycle. `measure` is parameterized — a baseline, a
           re-measure and a candidate-check are the same verb.
           The playback transaction lives INSIDE measure, with one
           owner: it is where every recorded incident in this
           inventory happened.
   REFUSALS  CLAMP      5 mechanisms / ~112 enforcement points — stops
             INTEGRITY  ~100 — cost is a re-measure; MUST still bank
             DISCLOSURE says it, never blocks
                                 │  (program, samples, sample_rate) + a dir
                                 ▼
 TRUTH LAYER — two packages, 15 analysis units. UNCHANGED. No upward
   import, each package against its own front end: audio_measurement takes
   neither consumer package, crossover_v2 takes no web
   (test_correction_boundary_ssot).
   62 of 79 in-product modules pure · 10 file readers · 7 live transport
                                 ▼
 TRANSPORT — the fan-in `correction` ring lane. Stereo, bit-exact
   (measured). Same lane for both front ends. Isolation bounded at
   2 channels, and it fails SILENTLY (MS-16).

 FENCE — AEC. The bridge's only reference tap is DOWNSTREAM of CamillaDSP
   (`jasper/cli/aec_bridge.py:263`), so nothing the engine does to the
   graph reaches the reference signal. One shared surface: the OUTPUT
   DEVICE CONTRACT — format/rate/channels/period/buffer (MS-7). Churn
   across it goes DOWN (twice per stimulus → once per session), so the
   risk direction is favourable. But `09 §2` warns the inventory is
   SILENT on AEC: treat that silence as unsurveyed, not as cleared.
```

### The engine, part by part

**One volume owner.** 18 production-reachable fader writers today (+2 test-only
= 20 in tree; 21 live surfaces that can change loudness, counting the 3
non-Camilla carriers), **9 colliding inside one crossover-v2 session** with no
owner arbitrating (`01`). They collapse to **4 claim kinds** — household ·
transient-duck · session-measurement · commissioning. Detail in wave 5.

**One session graph, installed once, and it must be all THREE things** — `09`'s
correction to the addendum's first draft (PC-8): **role-routed** (role → output
channel) · **crossover-free** (or every driver is measured through the crossover
the session is designing) · **per-driver protected** (the tweeter high-pass
**and** the soft-clip limiter together, on exactly the tweeter channels, proven
once by `_assert_program_graph_proven` before the first stimulus — MS-13).
Isolation does **not** come from the graph: it rides the WAV's channels, and the
lane was measured passing stereo bit-exactly with an idle channel at exact
digital zero (`08`). Three axes, named once so nobody flattens them (PC-7):
**lane = transport · graph = routing · WAV = isolation.** Per-candidate change
is then a cheap `patch_config`, which already ships twice as prior art — the
bass bench's "structural swap once, patch per candidate", and `/correction/`'s
`_load_measurement_baseline`.

**One capture record.** 102 persisted record types today, 4 same-fact
duplications, 24 orphan sites, 2 inverse orphans (`02`). Five blocks: identity ·
**place** · stimulus-and-path · honesty · **the curve**. Block 5 does not exist
today for a lateral pose, *"and it is the whole gap."* Detail in wave 4.

**The verb set — SETTLED by the owner, 2026-08-25** (full ruling and quotes at
§4 S1). Four verbs, one vocabulary, no second layer: **`measure` · `analyze` ·
`recommend` · `save`.** The engine's verbs **are** the loop's language, in words
anyone would understand. *"Measuring is measuring"* — baseline, re-measure and
candidate-check are one parameterized verb, so everything the code calls VERIFY
today is `measure` with different arguments plus `analyze`. Propose, prescribe
and recommend are one thing, and the plain word wins: **`recommend`.** Run and
Collect are *"just back to measure again"* and are not verbs. `save` is simple.

The inventory's argument for a fifth `Run` boundary (`00 §3.5`) is **superseded**.
The engineering fact underneath it survives, relocated: the playback transaction —
ready → admit → lock → play → restore, where every recorded incident in this
inventory happened — becomes a **named internal module inside `measure`**, a
first-class code boundary with its own contract, but **not** a vocabulary item.
Pipeline mechanics are *"just the mechanics of how we execute the verbs… the LLM
doesn't really care about"* them. Real seam, real owner, invisible to the LLM.

**How today's code regions map onto the four verbs** — write this down once, or a
fresh session reading §3's wave names will think VERIFY is still a thing:

| Today's region | New verb |
|---|---|
| capture walk · capture plan · spatial group | `measure` |
| VERIFY (prepare · grade · verdict) | `measure` (candidate-check parameterization) + `analyze` |
| the 15-unit analysis layer · round views | `analyze` |
| the prescriber CLI's `packet → propose → stage → status` *(existing subcommand names, left alone)* | `recommend` (already shipped, already decoupled — **do not re-extract**) |
| `persist_conductor_state` · the record writers | `save` |
| the apply/rollback transaction | none — it is not a verb, and it never moves |

**Named measurement specs are DATA, not code** (owner, 2026-08-25): *"we can
pass, like, do all of these measurements or do just these ones… different config
spec, whether it's a baseline measurement or a full cloud measurement or only
horizontal measurement."* A **PRESET is a saved parameter bundle for `measure`** —
`baseline`, `full-cloud`, `horizontal-only`, `design-axis-diagnostics`, and
whatever the campaign needs next. It may be authored by a person, by the runbook,
or by the LLM. **Picking one is picking parameters. Adding one is writing data —
it never touches the engine.** A preset that requires an engine edit is a design
error, not a new preset.

Presets compose **instruments with their natural scopes**, and the roster (§5)
already implies the scope fact: some instruments are **design-axis-only** — the
reverse-null is one act at one place — while others are **per-position** — the
per-driver sweeps run at every pose the preset names. A preset therefore carries
*which instruments* and *over which positions*, and the engine reads both as
parameters. This is the **fourth absorption test of the settled vocabulary**, and
it passes: presets needed no new verb. The running tally, kept here so it is
stated once — (1) the reverse-null, (2) MS-17's third mover, (3) §5's
six-instrument roster, (4) measurement presets. Four things the four verbs
absorbed without growing a fifth.

**`analyze` defaults to EVERYTHING the banked data supports** (owner,
2026-08-25): *"once you have measurements, I think all the analysis should just
kind of fall out… the analysis is relatively cheap, so you just may as well."*
Three stated properties, not accidents:

1. **The default is wholesale.** `analyze` runs **every** analysis whose input
   kinds are present in the banked records. Not a selected subset, not what the
   caller thought to ask for. Cheap compute is the reason the default can be
   generous; the reason it *must* be is below.
2. **A missing input is DISCLOSED, never silently skipped.** An analysis whose
   inputs are absent reports as **not-run, naming the missing kind** — *"no
   distortion analysis: no distortion-vs-level capture in this session."* Silence
   is what let the current defects hide.
3. **Every analysis is re-runnable offline, forever, without re-measuring.**
   Because records persist complete (ruling S3, wave 4a), a banked session can be
   re-analyzed by any future analysis that did not exist when it was captured.
   That is a **property of the record contract**, and it is the whole return on
   banking `DriverResponse` with phase.

*This changes no analysis unit.* The wholesale default lives in the **caller** —
the `analyze` verb deciding what to invoke — which is exactly the corollary §0
already carries: *do not decouple the analysis layer, replace its caller.* The 15
units still port whole and unedited; what changes is that something finally calls
all of them.

**This default is the direct fix for `13`'s largest finding class.** The gap
audit's recurring shape is *measured-but-unconsumed*: the room stack banks the
richest per-frequency curves in the tree and **nothing opens them**; distortion is
separated scrupulously and **no gate reads it**; `flat_spec_views.directivity_table`
computes measured per-angle directivity and **feeds a text renderer**;
`forward_model.predict_sum` predicts the complex sum per angle with **zero
production callers**. Each of those is an analysis that exists, has its inputs,
and was never run because nothing asked. **A wholesale default makes that class
structurally impossible to recreate** — the question stops being "did someone
wire this up?" and becomes "are the inputs in the bank?", which is answerable by
looking.

**Refusals, three sorts.** **CLAMP** stops — 5 named mechanisms, ~112
enforcement points; quote both numbers every time, because collapsing the 112 is
not the job and naming the 5 is. **INTEGRITY** is the class that needs a NAME —
~100 refusals protect the honesty of the evidence and have no doctrinal home,
which is how a nanny wore a costume for as long as it did. Adopt the principle
verbatim — *"A dishonest measurement is worse than no measurement"* — with the
owner's #2087 ruling as the discriminator: **would measuring again plausibly fix
it?** Yes → capture defect; refuse, **and still bank**. No → it describes the
room, the rig, or the result; disclose and recommend, never block.
**DISCLOSURE** never blocks. Plus the **STOP-RELAXER** pattern — three pieces of
production code exist only to let a mostly-right stop through, a census that
greps for `raise` cannot see them, and naming it makes #2935's fix obviously a
fourth one.

**The front ends are thin, and that is the whole modularity claim.**
Web-driven human measurement and LLM-plus-robot-arm measurement are thin front
ends over **the same four verbs** — in the owner's words, *"they should all use
the same plumbing."* A front end picks positions and candidates and shows
results. It does not own volume, install a graph, or decide what banks. The
wizard and the arm runner differ only in **who moves the mic**. If a change has
to be made in both, it belongs in the engine.

---

## 2. The contract — what must survive

These bind the plan. They come from `09-seam-contract-tests.md`, reproduced here
as the plan's contract section. MS-1…MS-6 and MS-13…MS-16 are hard constraints;
MS-7…MS-8 are tripwires to check before the design freezes; MS-9…MS-12 are
principles the consolidated code should carry forward.

**Hard constraints on the session graph and on WAV-channel routing:**

1. **MS-1 — Whole device contract, or none.** Any graph a measurement path loads
   must derive **every** `dataclasses.fields(ActiveEmitDevices)` field from
   `active_emit_devices(playback_device, topology=...)` and forward all of them —
   a subset is #2450/#2343/#2359/#2363 re-armed, and at session scope it poisons
   every stimulus instead of one.
2. **MS-2 — Both ends move together.** Under `shm_ring`, a graph's capture and
   playback halves move on the same rung or neither does, because a ring sink over
   the snd-aloop tap is digital silence with every daemon reporting healthy.
3. **MS-3 — One wire.** A ring-named lane carries `resolve_ring_wire(topology).sample_format`
   on *both* ends plus `RING_CAMILLA_CHUNKSIZE`, `RING_CAMILLA_TARGET_LEVEL`,
   `queuelimit: 1`, `enable_rate_adjust: false` — the box's program-lane default is
   the shear that halted jts3's arm attempt 2.
4. **MS-4 — Stimuli enter pre-DSP.** A stimulus may ride a *renderer-lane* ring
   (ingress into fan-in) but never the post-crossover `jts_ring_active_playback`,
   whose single-producer epoch takeover *admits* a stray writer where a raw `hw`
   device would refuse.
5. **MS-5 — Every ring-naming emitter asks the width.** Any new emitter that can
   name a ring device must call `_assert_ring_playback_width`, because the ioplug's
   attach compares channel count field-by-field and *crashes* the ring rather than
   refusing the config.
6. **MS-6 — No full-range graph on a roleful box.** `flat_program_graph_blocked_reason`
   must keep refusing the flat program lane on every topology that has an active
   ring, or a full-range program reaches a compression driver.

**Hard constraints the sibling tests add:**

13. **MS-13 — The measurement graph is role-routed AND crossover-free AND
    tweeter-guarded.** `_assert_program_graph_proven` is the emitter's fail-closed
    return contract — reference-closure plus three L0 tweeter proofs requiring the
    high-pass **and** the soft-clip limiter together on exactly the tweeter output
    channels — and a session-scoped graph must still pass it, once, before the
    first stimulus.
14. **MS-14 — Every stimulus plays at the declared level, proven, or not at all.**
    The fader is read back and proven before any audio, and a fader that cannot be
    proven refuses the capture rather than banking it — because
    `readmit_program_from_wav` admitted the program against that declared level.
    **This is the shape ruling S10 preserves**: it refuses to CLAIM, never to WORK —
    the stimulus still plays and the session still measures again.
15. **MS-15 — The lane wire is a boot-time fact with zero writers.** A measurement
    session must never write `JASPER_FANIN_RING_WIRE_FORMAT` or re-render the ring
    conf.d; that key is the fleet's only rollback lever off the wide wire, and its
    writer set is asserted empty across production Python, the installer, the
    deploy bins and the units.
16. **MS-16 — A stimulus WAV wider than the lane is silently downmixed, not
    refused.** `pcm.correction_ring_lane` is a `plug` over a `channels 2` slave, so
    channel-content isolation is bounded at 2 through that lane; anything wider
    belongs on the topology-derived active ring, reached by the arm ladder.

**One invariant this plan ADDS — not from `09`.** MS-1…MS-16 are reproduced from
the seam-contract pass. MS-17 is new, added by **owner ruling, 2026-08-25**:

> *"our cleaned up, tidied code is at its core AGNOSTIC of whether a robot arm
> took the measurement or a human took the measurement, and the systems that power
> both the human-guided measurement and the robot-guided measurement share as
> absolutely much as possible."*

17. **MS-17 — Mover-agnosticism.** The engine below the front-end seam contains
    **zero arm-specific and zero wizard-specific code**; the only difference
    between front ends is **who satisfies the "the mic is at position P"
    precondition** — the arm reports it, a person confirms it — and both produce
    an **identical `place` block** in the record, so **no analysis, gate, or
    record SEMANTIC may branch on mover identity.**

Three clauses that make it operable rather than aspirational:

- **Provenance may note the mover; nothing may act on it.** Recording *which*
  mover placed the mic is a fact worth banking. Reading that field to choose a
  code path is the violation. The line is: it may appear in block 3, never in an
  `if`.
- **The `prompt` field is the human path's artifact, and it is carried either
  way.** The arm-driven record keeps the field rather than growing a second
  record shape — one shape, some fields empty, which is the *opposite* of the
  union-type-with-null-columns mistake §wave 4 forbids, because `prompt` is one
  optional field on a shared block, not half a schema.
- **It is structurally testable, and PR 0d names the test.** The engine package
  imports nothing from the arm tooling (`experiments/usb-turntable`, `arm_walk`,
  `angle_capture`) and nothing from `jasper/web/` — an **AST import assertion in
  the same family as the truth layer's zero-upward-imports test**
  (`tests/test_correction_boundary_ssot.py`, the invariant `00 §7.1` calls the
  reason this refactor's risk estimate is low). Both front ends call the same
  `measure(position=...)`, and **a third mover — phone-guided, or whatever comes
  next — is added with zero engine edits.** That last sentence is the real test of
  MS-17, and it is the same test ruling S1 passed when the reverse-polarity null
  test needed no new verbs.

**Two pieces of evidence already stand under this.** The record half is proven:
this week's baseline rounds were **arm-driven** and banked `place` blocks with the
same fields a prompted human flow produces — the shape is not a hope, it is what
`captures/postfix-baseline-2026-08` already contains. And the S8 recipe's
governing constraint — *same drive voltage, nothing touched between measurements*
— is **mover-independent by construction**: it constrains the fader and the graph,
neither of which knows who moved the mic.

**Tripwires — check before the design freezes:**

7. **MS-7 — The chip-AEC alignment artifact is edge-bound.** A commissioned K is
   valid only for the exact `output_format/rate/channels/period/buffer` it was
   measured against; any change to that geometry must park the box with
   `CommissionRequired`, never degrade quietly. (The reference *signal* is safe —
   the tap is downstream of CamillaDSP — so this is geometry only.)
   **MS-7 survives ruling S10, and the reason is scope, not exception:** chip-AEC
   geometry parking is a **clamp-class call owned by the AEC side**, which this plan
   fences out (§0 non-goals, §1's fence line). We neither relax it nor endorse it —
   we state that it is not ours to demote. If the other program reconsiders it under
   S10's principle, that is their call to make on their surface.
8. **MS-8 — A tone must fit its lease.** Anything played under a single
   un-refreshed fan-in test lease stays shorter than `mux.FANIN_TEST_LEASE_SEC`
   (60 s), or it adopts the measurement window's refresh loop instead.

**Principles the consolidated code should carry forward:**

9. **MS-9 — A secondary DSP instance fails closed to silence, never to a reboot**
   (camilla#2 carries no `StartLimitAction`; only the always-on camilla#1 owns a
   recovery handler).
10. **MS-10 — A blocked graph-repair decision leaves the statefile byte-for-byte
    untouched** — never half-act, never fall back to flat.
11. **MS-11 — The fan-in gate is owner-scoped**: a select and its release name the
    same owner, and an *indeterminate* select still releases, so one surface can
    never steal or strand another's gate.
12. **MS-12 — Commission-tone orchestration has exactly one owner module**, shared
    by both operator surfaces as the same function objects and imported constants.

### Three contract items the wave PRs must execute, not just respect

**PC-3 — Re-point the two graph-emit guards; never drop them.**
`test_the_crossover_v2_program_graph_follows_the_arm_in_both_directions`
(`test_ring_active_endpoint.py:2410`) and the
`bind_production_play(crossover_v2 CHECK/MEASURE)` entry in
`test_every_emit_devices_field_reaches_the_emitter` (`:2603`) are the **only**
tests holding the measurement path's emit to `active_emit_devices`, and both bind
the per-stimulus call site this plan deletes. Add the session-graph emit site to
both **in the same PR**. MS-1's blast radius grows under this change — a
half-derived device block poisons one stimulus today, every angle and retry and
driver tomorrow — so the guard gets *stronger*, not deleted with the site it
happened to watch.

**PC-4 — Two hard-coded counts break on a new emitter.**
`test_the_width_refusal_actually_fires_through_an_emitter` asserts
`len(call_sites) == 5`; `test_the_emitters_default_to_todays_literals_byte_for_byte`
asserts `len(takes_the_pair) == len(emitters)`. Both are deliberate — the count
*is* the claim. Update them **and** satisfy them (MS-5).

**PC-10 — State the 2-channel bound before a 3-way finds it.** It fails
*silently*: `plug` downmixes rather than refusing. Nothing is broken today (the
emitter refuses non-2-way presets outright), so say the bound is deliberate and
name the topology-derived active ring, reached by the arm ladder, as the path for
anything wider.

*One vocabulary note, so the plan does not mint a second set.* `04`'s census
sorts refusals into **five** classes — CLAMP · INTEGRITY/LIVENESS ·
QUALITY-IN-COSTUME · STOP-RELAXER · PROTOCOL (excluded). The three sorts above
map onto it exactly: CLAMP is CLAMP, INTEGRITY is INTEGRITY/LIVENESS, and
**disclosure is the destination** a demoted quality-in-costume refusal becomes,
not a fifth bucket. PROTOCOL (~35) is out of the argument; STOP-RELAXER is a
pattern, not a class. Use `04`'s five when counting, these three when deciding.

---

## 3. The waves

Nine waves. Small PRs, one concern each.

**The standing sequence, per ruling S5 — every wave, no exceptions:**

```
build new  →  prove  →  delete old        (all three inside ONE wave)
```

No fallback flags. No duplication. No coexistence windows. Where a wave keeps an
old route alive for a moment, it is a **proof bracket** and it dies in that same
wave. **What "prove" means scales with what the wave changes:** waves that alter
what the box actually does (4, 5, 6) take the acceptance run — the r1/r2
reproduction inside the 0.37 dB noise floor (§5 row 9), which is a **sanctioned
S11 validation act**, not a licence to tune. Waves that move code
without changing measurement behaviour (0, 2, 3, 7, 8) prove with the class-A
suite. *That split is the conductor's scoping of the rule, not the owner's
words — say so, and let the owner collapse it if he meant a hardware run every
time.*

**The review gate is the charter's Review policy** — scale ceremony to risk:
mechanical and doc work gets author plus a sanity look; ordinary production code
gets **one** adversarial review pass; only a change to the closed clamp list in
the charter's **Non-negotiables** gets a real review. (That charter replaced
AGENTS.md's right-sizing directive; the substance is unchanged — see §6 R9a.)
Where this plan spends more than that, it says so and why.

**The merge discipline is now ENFORCED, not just agreed** (governance report, 2026-08-25). Branch protection went ON at **#2942**: the `ci` context is required with `if: always()`, `enforce_admins=true` so there is **no admin bypass**, and force-push and branch deletion are blocked, with conversation resolution required. **No red merge is possible** — a wave cannot land on a broken tree even by accident. One thing protection does *not* do: **strict / up-to-date is OFF**, per the 2026-08-07 ruling, so **serializing a batch landing is still a matter of discipline**, not machinery. When a wave lands several PRs close together, order them yourself; nothing will do it for you.

### The deletion rules — binding on every wave that deletes a test

Settled by ruling S7. **The unit of deletion is the TEST FUNCTION, not the
file.** File-grained classes (`10`'s A–E) are a *map*, not a verdict — a mixed
file is the normal case, not the exception.

**The mechanical pass, run per mixed file before any deletion:**

| | Step | Why |
|---|---|---|
| **a** | List the symbols each test exercises; mark whether the subject **survives in the new engine**. | The subject's fate decides the test's fate. Nothing else does. |
| **b** | Grep the file's fixtures and helpers for **EXTERNAL importers**. | Sibling files reuse test infrastructure — `test_fanin_coupling_reconcile.py`'s recorders feed two siblings, and `crossover_v2_fixtures.py` has many. **Deleting a "dead" file can break a class-A survivor.** |
| **c** | Any assertion touching **out-of-zone symbols** (camilla, mux, volume_coordinator, fanin, control) → the **SHARED-SEAM LIST**. | Coordinated with the audit program before deletion. **Never deleted unilaterally.** |
| **d** | Any docstring citing a **dated incident or issue number** → the incident pin is re-pointed or kept. | **Never deleted with the mechanism.** |

**Then:** functions whose subject dies and which trip none of (b)–(d) → **delete
with the subject, in the same PR.** Surviving invariants → the **class-B rewrite
queue**.

**NEVER window-sample a big file.** The audit agent's own verification caught a
wrong verdict drawn from a 190-line window of a file where whole-file measurement
said **35 of 160**. Skim the **full def-list plus docstrings**; deep-read only the
functions that flag.

**Default-aggressive is licensed — for dying happy-path choreography ONLY.**
Three exceptions, each of which is class B wearing a C or E costume:

1. **FAILURE-BRANCH pins** — refusals, races, cancellation and retry, stall
   recovery. **The proving ground exercises none of these**, so a green r1/r2 run
   says nothing about them. A C/E failure-branch test is deletable **only** if the
   new engine has no equivalent failure mode; otherwise it is class B in disguise
   and goes to the rewrite queue.
2. **DEADNESS-ENFORCING tests** — they die in the **same PR** as their subject,
   never before. A deadness pin deleted early stops enforcing deadness during
   exactly the window when something could resurrect.
3. **CROSS-PROCESS / cross-language pins** — grep the subject's **literals**
   repo-wide before deleting their pins. Agreement tests hide in unrelated files;
   `09`'s verdict table is a worked example of exactly this shape across five
   languages.

**The deletion checklist, from the audit agent's verified failure patterns:**

- **Read assertions, not names.** Ratios and name-greps *propose*; only reading
  *disposes*. (Same discipline as R2, and the same failure shape `04` recorded
  three times against itself.)
- **"Is it in a CI lane" and "is it referenced" are two separate checks.** Do
  both. Neither implies the other.
- **Dedup by CONTENT, never by filename pattern.** Their flagged "redundant
  family" of incident-replay tests turned out to document a **different real
  incident each** — and that family is **in our zone**.
- **Read every swept seam file before classing it C or E.** `09` already read the
  six the audit named (5,556 lines) and its verdict table stands; the rule applies
  to the **unidentified rest** — six of "roughly a dozen" remain unnamed, per §6.
- **Check every deletion against the invariant→pin table** (wave 0, PR 0d). A
  deletion that would leave a must-survive invariant with no named pin is blocked
  until the replacement pin is written.

**The SHARED-SEAM LIST, as reconciled.** Standing entries, each needing an
explicit acknowledgement rather than silence: `rust/jasper-fanin/**` (frozen after
their host-compliance PR until the stereo tap re-runs) · `doc-map.toml` zone rows
(removals ride our PRs) · **the volume surface** for the duration of wave 5 · and
**`experiments/usb-turntable`**, the robotic-arm driver every `measure` angle
depends on, whose promotion into `jasper/` is the audit program's owner gate and
whose re-verification before any campaign is ours. Terms in §6 R5.

**Citation riders — fix them in the wave that touches the file, never alone.** The governance charter deleted the old AGENTS.md section headings, so roughly **42 quoted-name citation sites now dangle repo-wide** — invisible to link checkers, because they cite a heading by name rather than by link. **Our zone's share, verified by grep at `origin/main`:** `jasper/web/correction_crossover_v2.py` · `jasper/audio_measurement/correction_lane.py` · `tests/test_active_speaker_design_draft.py` · `docs/HANDOFF-active-speaker-dsp.md` · `docs/testing-tooling.md` (**2 sites**) · `scripts/tuning-llm-live-check.py`. Each gets its citation repointed **by the wave that already opens that file — a one-line rider, noted in the PR body. Never a standalone PR**, which would be churn for its own sake and exactly the ceremony the charter's Review policy cuts. Two dispositions that are not riders: `tests/test_docs_handoff_freshness.py`'s citation **dies with the file in 7h**, and the frozen banks' citations (`correction-ux-wave3/`, `bass-extension-waves/`) **stay frozen** per 7f's fencing — they are primary sources and are supposed to be stale. The **other ~30 sites belong to the parallel programs and are out of scope**; stated so nobody reads our six as the whole set.

> **CLOSED at wave 8, and the six-file list above is spent.** `91a50fc56`
> (#2948) repointed 43 sites across 38 files and took **all six** of ours. What
> the AGENTS.md-shaped sweep could not see is the residue: a citation may route
> through **`CLAUDE.md`**, which is now a ten-line `@AGENTS.md` shim with **zero
> sections**, so a quoted section name there dangles exactly the same way and
> matches no `AGENTS.md` grep. Our zone held **two**, both in
> `docs/testing-tooling.md` (`:1096` → the Evidence-first default, `:3325` →
> non-negotiable 7), fixed in wave 8's docs PR. Repo-wide at HEAD: **29 live
> quoted-name citations, 28 dangling, 27 of them the parallel programs'** — the
> "~30" above holds. **Sweep discipline, five-for-five this campaign: 13 of the
> 29 live sites WRAP ACROSS A NEWLINE** and are invisible to single-line grep,
> which is the mechanism behind every false "already fixed" verdict here. Join
> line pairs before matching. **Not in scope and not a defect:** the doctrine's
> **letter map survives** (`measurement-loop-doctrine.md:224-248`) even though
> S9 deleted the table above it, so those citations all still resolve — the doc
> kept the map precisely so they would. Counted as a **floor, not an estimate**:
> **at least 75 live sites across 26 files** match a strict wrap-safe
> `deviation (a)`…`(i)` grep; looser citation shapes would only raise it. One
> judgment call is **left open for the owner rather than guessed**, and it is enumerated
> rather than estimated: **10 live sites** attribute a *"no-silent-failure rule"*
> to the charter, **5 in this zone** —
> `jasper/web/correction_crossover_v2.py:4962` ·
> `jasper/active_speaker/crossover_v2/accountability.py:666` ·
> `tests/test_correction_crossover_v2_endpoints.py:3457` and `:8882` ·
> `tests/test_crossover_v2_conductor.py:8541` (wrap-only) — and **5 outside it**:
> `jasper/wake_fusion.py:72` · `jasper/correction/autolevel.py:309` ·
> `tests/test_correction_autolevel.py:392` ·
> `docs/two-stage-commission-flow-plan.md:440` and `:696`. The charter has **no
> rule under that wording**, and its nearest survivor, non-negotiable 6 **No
> silent deafness**, is scoped to *wake-response cues* — not a wizard's disabled
> Apply. Repointing them would **widen a non-negotiable by prose edit**, which is
> not a rider's authority, so they are flagged and left alone.
>
> **RESOLVED — owner ruled 2026-08-26: DROP the attribution, do not grow the
> charter.** Two corrections came with it, both from `REFACTOR-CUTOVER-2026-08.md`
> Appendix A. The count is **13, not 10** — a wrap-safe sweep found three more
> sites spelling it *"no silent failure paths"*, one of them wrapped across a
> newline. And the sites are **two classes, not one**: four are cue-shaped and
> repoint honestly to `docs/extensibility.md:87-89`, which carries the wording
> live; the other nine claim a greppable-disclosure rule that has never existed
> in any revision, and simply lose the parenthetical. **The closed list is not
> touched either way**, which is what made the drop safe. Executed as one
> comments-only sweep rather than the wave riders proposed here.

### Wave 0 — Free the class-A suite. Zero design decisions.

**Goal.** Make 149 test files portable and cut god-file-to-god-file coupling in
half, without deciding anything.

| PR | What |
|---|---|
| 0a | Move the 23 symbols the class-A suite imports out of `crossover_v2_flow` into the package; give the two flow-owned constants (`VERIFY_TOLERANCE_DB`, `DEFAULT_CLOUD_MEASURE_POSITIONS`) a home in `crossover_v2/contracts.py`; repoint the two that are already package-side re-exports. |
| 0b | Quarantine the single conductor-building test in `tests/test_audio_measurement_program_analysis.py` (`test_configured_path_matches_legacy_through_analyzer_and_fitter`, a four-line local import at `:392`), making the census's largest class-A file (8,170 lines, 187 tests) session-free. |
| 0c | Repoint the two re-export doors — `refusal_copy`'s 67 `X as X` and the `journey` phase vocabulary — at their owning modules. |
| 0d | **Build the invariant→pin table** (added by ruling S7). Turn §2's **17** must-survive invariants from a list of intentions into a table: **invariant → the NAMED test function that pins it.** Seventeen lookups; it is cheap and it is done before anything moves. **MS-17 arrives with no pin at all** — its AST import assertion (engine imports nothing from `experiments/usb-turntable`, `arm_walk`, `angle_capture`, or `jasper/web/`) is **written in this PR**, modelled on `tests/test_correction_boundary_ssot.py`, and it is the first row filled. **Any row that comes back with no name gets a pin WRITTEN — before the old pin is deleted, and before any wave touches its subject.** As waves land, each row's name is updated to the class-A survivor or the rewritten class-B test that carries it. Every deletion wave checks against this table. |

**Counted.** ~24 edits (`10` step 0). **−271 lines** (`06 §Judgment 5`). Frees
**19 class-A files / 26,776 lines** and removes **23 of the web file's 50** flow
imports. Tests: **0 ported, 0 rewritten, 0 deleted** — 15 test files' imports
repoint. Plus 0d: **17 lookups, and one new pin written for every invariant that
comes back unnamed** — the only lines this wave adds, and the cheapest insurance
in the plan.

**One thing this wave does NOT buy.** The 271-line door win does not translate
to the test surface: exactly **2 files / 644 lines** import *only* door symbols.
`PHASE_*` (51 imports) and `REASON_REGISTRY` (13) almost always arrive alongside
`CrossoverV2Session` (`10 §Two things the ordering must not inherit`). Book the
production win; do not book a test win.

**One symbol is not free.** `spec_report_for_predicted_sum` is caller-side glue,
part of the ~1,500-line irreducible core, and must land in the new engine before
the A suite that calls it can run there (`10`).

**Verify.** The class-A flagship suite stays green — `10` ran three of them at
`e064fa43d` and got **410 passed, 24 skipped in 10.6 s**. Reproduce that number.
**Gate:** mechanical — author plus a sanity look. **Rollback:** `git revert`;
these are moves.

### Wave 1 — The engine skeleton, and its test double shipped WITH it.

**Goal.** A session object that can own a graph lifetime, a volume claim, and a
record — and a `FakeSeams`-equivalent so tests have somewhere to land.

**Carry the COMPLETE mic-only parameter surface here, with stubs** (ruling S12) —
`regime=near_field`, `position_axis=vertical`, distortion-vs-level,
`polarity=inverted`. Build it in this wave, not later: the whole point of S12 is that
**the API shape never changes when a capability lands**, and a surface that grows
per-capability is the thing it forbids. Each unimplemented regime returns a named
not-implemented disclosure in the same shape as the analyze default's missing-input
wording (§1). **New-hardware regimes — impedance — stay out.**

**The finding that makes this wave non-optional.** `tests/crossover_v2_fixtures.py`
is **1,948 lines imported by 26 test files totalling 57,079 lines**, and **21 of
the 26 construct a `CrossoverV2Session` through it**. Only 2 of the 26 (2,906
lines) would survive without a session harness. `10` is blunt: *"the new engine
has to ship a `FakeSeams`-equivalent on day one or 54,000 lines of test have
nowhere to land. Budget the test double as part of the engine, not as a
follow-up."* The repo has already paid for skipping this once — the fixture file
exists because `test_crossover_v2_conductor.py` had become a de-facto fixture
library that 18 modules imported 25 symbols from, which made a 12,680-line file
undeletable.

**Counted.** This wave is **net POSITIVE lines** and that is correct under rule 1.
Reference size for the twin: the 1,948-line fixture it replaces. Gates 57,079
lines of test. Tests: 0 deleted.

**This is not scaffolding, and rule 1 is satisfied.** The twin is **permanent
test infrastructure for the permanent engine** — it replaces
`crossover_v2_fixtures.py` one-for-one and outlives every wave. Nothing here is
built to be thrown away, which is exactly what *"we're not investing in systems
we're going to be deleting"* requires.

**Verify.** The twin builds a session-equivalent for the 24 importers that need
one; the 2 that do not stay green untouched.
`test_crossover_v2_verification.py:2219` asserts `"crossover_v2_flow" not in
joined` — **it must survive verbatim and be pointed at the new engine's package
in this wave** (`10`). **Gate:** one review pass. **Rollback:** `git revert`;
these are new files, and the old fixture is deleted in the same wave that its
last importer moves.

### Wave 2 — The verify REGION lifts first (it becomes `measure` + `analyze`).

**Goal.** Prove the strangler works on the code region with the lowest coupling.
*"VERIFY" here names a region of today's code, not a verb* — under ruling S1 it
lands as `measure` (the candidate-check parameterization) plus `analyze` (the
grading). See §1's mapping table; the wave names below are the inventory's labels
for today's tree, kept so the citations line up.

**Why this region first.** Its package half is already carved (4,769 lines); the flow file
imports 1–3 symbols each with **zero dotted call sites** for four of five; the
middle's verify code is contiguous and reads only `_verify_*` attributes — **18
of the 102**, none shared with measure. One route, one preparer, one grader. It
lifts on a single seam: **`(applied_candidate, entry_baseline, capture) →
verdict`** (`00 §5.4`, from `06 §Judgment 3`).

**Order inside the wave** (`10 §The VERIFY-first order`):

| PR | What | Lines |
|---|---|---|
| 2a | Rewrite `test_crossover_v2_entry_baseline.py` against the new seam — and fix `02`'s duplication #2 in the same edit (the only durable full copy of the entry baseline lives in an overwritable file). Same edit, both jobs. | 613 |
| 2b | Re-host `test_crossover_v2_round_wiring.py`'s restore/undo/anchor honesty **without its subject moving** — the apply/rollback transaction never moves. `10` calls this the trickiest B in the lane; do it while the context is fresh, not last. | 3,655 |
| 2c | Lift the VERIFY slice: ~132 verify-named tests (67 + 33 + 32) out of three god-file suites occupying 31,729 lines. | — |

**Counted.** VERIFY is ~7,403 lines total, **~2,634 of them in the middle** —
that is what leaves the god files. Per-verb test surface: 54 files / 70,102
lines. Tests: ~132 rewritten, 0 deleted.

**Verify.** Class-A green. **Gate:** one review pass. **Rollback:** `git revert`.
Per rule 3 the old route is a **proof bracket, not a fallback** — it lives only
until the new route is green in the same wave, and then it is deleted in that
wave. It never survives into the next one.

### Wave 3 — The rest of the strangler, in rank order.

| Rank | Target | Lines | Note |
|---|---|---|---|
| 2 | MEASURE — spatial group close | 1,219 session + 962 flow | `spatial.py` is already the destination and already takes 27 call sites. The seam exists; the work is *finishing* it. |
| 3 | PERSISTENCE | 2,649 web | `persist_conductor_state` is **854 lines** — the single largest function in either file. Pure serialisation of state the session already enumerates in 102 named attributes. "A schema writer with no schema." Highest-density mechanical win; needs no audio judgment. |
| 4 | MEASURE — capture walk + capture plan | 969 + 1,102 | Last: where `admission`'s 27 sites live and where the retry budget, the relay contract and the phase machine meet. |
| **never** | apply / rollback transaction | 1,185 web | Not a target. Ever. |

**Counted.** 6,901 lines leave the god files in this wave; with wave 2's 2,634
that is **9,535 of 23,022**. Tests: class-B rewrites — SPATIAL 30 files / 27,165
lines, PERSIST 20 / 19,023.

**Verify.** Class-A green after each rank. **Gate:** one review pass per rank.
**Rollback:** per-rank revert; ranks are independent.

### Wave 4 — One capture record.

**Goal.** Bank the curve. Stop re-deriving it. Make place a field.

| PR | What | Counted |
|---|---|---|
| 4a | **Bank `DriverResponse`, magnitude AND phase** (owner-settled — see §4). Five lines at `crossover_v2_flow._retain_lateral_pose`. Deletes `round_views.verify_pose_curve`'s deconvolve→gate→smooth→resample block outright, and the campaign's `derive_position_curves.py`. | **+5, deletes more than it adds** |
| 4b | Duplication 1 — bundle take vs ring sidecar: one builder, one index (`take_id`), delete the `ENABLED` gate. Five offline readers stop globbing a second index. | M |
| 4c | Duplication 4 — `AdmittedRegionCapture` vs `AdmittedCaptureProof`: one shape, `post_apply` becomes a field. | S |
| 4d | Duplication 2 — entry baseline full vs digest: move the full copy to a write-once artifact. *(Landed in wave 2a if taken there.)* | S |
| 4e | Duplication 3 — room `position_analysis` ×3: one owner, two views. Kills four-way `or` chains in readers. | M |
| 4f | Deletions: the dump-ring **sidecar** as a second capture record (its four unique blocks move into block 4; the ring keeps its WAVs) · `prediction.json` · the round-root `candidate.json` · the 72-point `fr_curve` third copy · the five dead commissioning publishers and their typed schemas · `CalibrationCurve.phase_deg` (delete or apply — "parsed, stored, reloaded, migrated, never applied" is a decision, not a pending task). | 24 orphan sites |
| 4g | **The commissioning lane is being REPAIRED, not abandoned** — the owner settled #2202 as *fix*, so this is now the **producer path, not the deletion path.** **The receipt half is done**: the eligibility receipt records `proven_at`, `proven_by_build`, `capture_refs`, and a topology-plus-microphone `hardware_identity`, and a receipt an older JTS minted now discloses as `active_commissioning_receipt_superseded` rather than as damaged bytes. **The producer half is blocked on inputs that do not exist, and that is the row's real cost.** "Nothing instantiates `SummedCaptureProducer`" is true but cheap; three greps say why it is not a wiring job. **(1)** That class takes a `RawCaptureTransport`, and **no production implementation exists** — repo-wide the alias has exactly two references, its own definition and its own constructor parameter, and nothing outside its test file has built a `RawCaptureResult`. **(2)** Production's only admitted-capture door is the **driver** relay door (`record_driver_capture` → `promote_isolated_driver_capture`); `jts_active_driver_capture_admission_handoff` is the only `ADMISSION_HANDOFF_KIND` in the tree and there is **no summed sibling**. **(3)** The v2 cloud-position captures #2998 wired into `bundles.register_capture` carry no capture admission at all — that lane uses `program_admission`, which mints **no `ArtifactIdentity`, no generation artifact, no playback artifact** — so promoting one would mean fabricating the admission the receipt requires. **So the re-arm is a new build against the relay in the live lane's promoter shape, not a composition of the stranded class**, and it needs a phone and a speaker to prove — which S11 excluded until a commissioning run was added to the sanctioned list explicitly. **That sanction now exists: S11 act 6, owner-ratified 2026-08-26.** The build still has to land first — the act proves a producer, it does not build one. **Two contract facts the build must carry.** *There is no post-apply path conflict to resolve*: the producer's `post-apply/{attempt_id}/{issuance_id}/{ordinal}` prefix locates the three CHILD artifacts (`raw.wav`, `analysis.json`, `quality.json`), while the reader's `post-apply/{target_fingerprint}/repeat-{ordinal}.json` locates the `AdmittedCaptureProof` ENVELOPE — `_reopen_capture` parses it with `AdmittedCaptureProof.from_mapping` and then reaches every child through `reopen_artifact(identity)`, never by reconstructing a path. The gap is **one missing write** — publish the proof `capture_post_apply` already returns — not a renamed prefix; changing the prefix would be a no-op for the reader and would break the producer's own tests. And **4h's `complete.json` writer transfers into this row**: `publish_complete_commissioning_evidence` has **zero production callers** (three test sites only), no production `CommissioningTransition` emits `to_state="measured"` (all fourteen sites emit protected/blocked/candidate_ready/blocked_live_state_unknown/rolled_back/applied_unverified/verified, two of them pass-throughs), so its live readers — `measured_candidate`'s complete-evidence reopen, `commissioning_evidence_store.reopen_complete_commissioning_evidence_anchor`, and `CommissioningEvidenceHost.status` — are unreachable until the producer feeds them. Both land downstream of the same missing transport; neither can ship as an orphan ahead of it. **The free −2,089-line deletion this plan previously booked stays withdrawn** — see §5's net-lines table and §6. **Ruling S10 governs the shape: the receipt RECORDS what was proven and never FORBIDS** — since #3005 and #3029 a receipt that cannot be produced leaves the lane working and says so loudly. | **+producer, −0** |
| 4h | **Fix, do not unify, the two inverse orphans.** `runs/{run_id}/complete.json` (six production readers, writer callable only from three tests) and post-apply `repeat-{ordinal:04d}.json` (**no writer anywhere, tests included**). A reader whose writer does not exist stays broken whatever the record shape becomes. Warning that travels: if anyone re-arms a `protected → measured` transition without restoring a `complete.json` writer, `commissioning_host.status()` starts raising on every poll. **The `complete.json` writer half moves to 4g.** Nothing in production transitions to `measured` — all fourteen `to_state=` sites emit other states — so restoring that writer is not a record-shape fix but the producer build itself, fed by the same region evidence 4g must first be able to capture; it cannot ship ahead of 4g's missing capture transport without becoming the orphan class #3045 deleted. **The post-apply `repeat-{ordinal:04d}.json` reader stays exactly as it is** — it is a live evidence gate, and 4g's trace found its writer is one missing publish of a proof `capture_post_apply` already returns, not a path contract to renegotiate. | 2 |
| 4i | Give the room stack its reader. It banks the richest per-frequency curves in the tree (`analysis/{stem}_response.json`) and nothing opens them; `recompute_bundle_summary` re-derives the whole chain from raw WAVs. | free |
| 4j | **The little measurement database** (owner-added, 2026-08-25). A **small SQLite index over the banked record files**: session id · kind (`baseline` / `candidate` / `verify`) · position · candidate id · timestamp · record path. Nothing else. House precedent to copy: [`jasper/wake_events.py`](../jasper/wake_events.py) — SQLite rows over banked WAVs, already shipped and already the pattern this repo trusts. | small |
| 4k | **The level fact — one definition, one estimator** (unblocked by ruling S8). `solve_branch_trims` **becomes THE level fact**. `driver_core_level_db` is **demoted to the starting-estimate role and KEPT**, with its delta against the handover level **disclosed**. The comparator stops asking whether two estimates of one quantity agree — *they never measured one quantity* — and starts disclosing "the handover level and the passband estimate differ by X dB, which is expected for a sloped horn." **Depends on 4a**: any consolidation is a WAV re-analysis campaign until `DriverResponse` is banked. | 2 estimators · 1 comparator · 1 tolerance · 2 record fields · 1 journal payload; **no orchestration touched** |

**The unified record is five blocks:** identity · **place** (`position_deg`,
`position_axis`, `mark_distance_m`, `offset_cm`, `at_mark`, `role`, `prompt`) ·
stimulus-and-path (the `provenance` block — *"the 8.712 dB level bug was two
fields of this block disagreeing"*) · honesty (the whole
`analysis_diagnostic_summary`, gate, integrity, frame ledger) · **the curve** on
a shared log grid. Blocks 1–4 already exist split across two writers; merging
them is mechanical. Block 5 for the lateral walk is the only genuinely new
persistence.

**On 4j — why an index, and how small "little" is.** The owner's reason: *"we
should bank all information for every measurement, but we might want like a
little database to organize them… baseline measurements and then measurements
across three candidates and then verify measurements, that's a lot of
measurements to just be floating around… nicely normalize all of the
information."* One campaign is genuinely dozens of records across positions and
candidates, and today the only way to find one is to glob a directory.

Three constraints keep it small and keep it honest:

1. **It is an INDEX over files, not a second store.** The banked record files stay
   the single source of truth. Every column is derivable from them, so the
   database must be **rebuildable by rescanning** — losing it loses zero
   information, and it can never disagree with the files for long. This is the
   `wav_sha256`-as-verifier-never-index discipline applied one level up.
2. **It is not a management system.** Browsing, curation, and deletion of stored
   measurements stay **future scope** — already stated in §0's non-goals, and
   ruling S3 says so directly: *"right now, let's just save the information."*
3. **"Little" is the owner's word.** Six columns, one table, one writer, one
   reader. If a design conversation starts adding a schema migration story, it has
   left the brief.

One vocabulary note so 4j does not look like it contradicts S1: `baseline`,
`candidate` and `verify` are the **kinds of measurement**, which are exactly the
parameterizations of the one `measure` verb. The `kind` column is where
*"measuring is measuring"* becomes visible in the data — one verb, one record
shape, one index, three arguments.

**On 4k — what the engine actually computes.** The definition is settled (S8);
this recipe is **engineering synthesis, labelled as such** — the owner-commissioned
research supplied the definition and its Stage 1–3 method, and the rendering into
what `measure` and `analyze` implement is the conductor's, not a cited finding.

| Stage | What |
|---|---|
| **measure** | **Same drive voltage across every per-driver measurement; no gain is touched between them.** |
| **analyze** | **Energy / RMS average of 1/6-octave-smoothed magnitude over ±1 octave around Fc, computed AFTER the target filters are applied.** Not before — the level fact is a property of the filtered traces. |
| **set** | Trim to **≤0.5 dB**. The research's phrase: it is *"free in DSP."* |

**The `measure` row is not a new requirement — it is what waves 5 and 6 already
deliver**, and that is a useful check on the architecture: one volume owner with
one declared level (wave 5) plus MS-14's fader proven before every stimulus is
precisely "same drive voltage, nothing touched between measurements." The recipe
and the refactor were derived independently and agree.

**Two tolerances, two jobs — say so, or someone will collapse them.**
**≤0.5 dB is SETTING precision** (how close the trim must land).
**`REALIZED_LEVEL_MATCH_TOLERANCE_DB = 3.0` is a DISCLOSURE trigger** (when to
tell the user the realized match drifted). They are different questions; the
3.0 stays.

**Two constraints on the statistic, for the record.**
*Slope:* with LR4 the sensitivity to level error concentrates **at Fc**, so the
matching band is ±0.5–1 octave and the null test carries the weight; shallow
crossovers widen the band to **±1.5 octaves** and the broad sum carries it.
*Directivity (Toole):* where woofer beaming and horn directivity mismatch,
on-axis, listening-window and power-response ratios differ — **there is no single
correct level.** The tool must state **which axis it matched** and disclose the
compromise. This is the same physics the campaign's graphs page already recorded
as its beaming finding; connect the two rather than re-deriving it.

**Wave 4-adjacent, newly assigned: `jasper/bass_extension/` and #1738.** The
audit program's boundary hands this package to us — it sits in our zone
(audit-program reconciliation, 2026-08-25). It is **one owner decision and one
PR**, not a wave:

- **The decision is the owner's: #1738, wire it or delete it.** The package is
  ~4,600 lines plus 715 lines of test.
- **Their verifier's negative proof rides the PR** — the deadness is
  *structurally enforced by the package's own tests*, which is a stronger claim
  than "no caller found" and exactly the kind of evidence R2 says a deadness
  claim needs.
- **The deadness-enforcing tests die in the SAME PR as their subject**, per S7's
  exception 2. Never before, never after.

Sequence it beside wave 4 because it is a record/analysis-adjacent package, but
it blocks nothing and nothing blocks it.

**Two things this wave must not do.** Do **not** merge `lateral_pose_record` and
`cloud_position_record` into one shape with optional columns — the tree has
reasoned about this twice and reached the same answer: a common core plus a
role-tagged extension, never a union type with half its columns null. And
`wav_sha256` stays the **verifier, never the index** (`02 §Judgment`).

**Verify.** Class-A green, then the acceptance run (§5 row 9) before the wave
closes — this wave changes what the box banks.
`test_crossover_v2_accountability.py` (527 lines, class A) **goes red on
purpose** if the prediction-gate reference fix lands here — name it in the PR body
(`00 §R2`; second site at `test_crossover_envelope_v2.py:3103`). **Gate:** one
review pass; the excitation-ledger and driver-cap fields inside block 4 are on
the clamp list, so any PR touching those gets a real review. **Rollback:**
`git revert`. The new record is written, readers are flipped, and **the old
writer is deleted in the same wave** — per rule 3 the two shapes never coexist
past the flip.

### Wave 5 — One volume owner.

**Goal.** 18 production-reachable fader writers → **one owner exposing 4 claim
kinds**: household · transient-duck · session-measurement · commissioning.

| PR | What |
|---|---|
| 5a | Stand up the owner. It owns: the write door (`_coerce_main_volume_db` stays as defence in depth, and the owner becomes its **only** caller); one declared level replacing five overlapping notions (`listening_level`, `measurement_volume_db`, `locked_main_volume_db`, `SolvedLevel.main_volume_db`, `fader_db`); a **ranked claim** replacing seven ad-hoc gates; **one** confirm tolerance replacing `0.05`, a second independent `0.05` literal, and `1e-6`; and the release algebra `min(reference, current + depth)`. |
| 5b | Route the 12 legitimate-intent writers through it: W1, W2, W4, W5, W7, W9, W10, W11, W12, W14, W15, W16. |
| 5c | Close W18's bypass — `jasper/cli/aec_tune.py:_camilla_set_volume` **never sees the 0 dB ceiling clamp** today. There are **2 hardware doors, not 1**; this makes it 1. |
| 5d | Merge W11 (`CrossoverLevelLease`) + W12 (`MeasurementSession`) + W7's third schema — one question, three durable schemas. |
| 5e | Delete W3 (the 1 Hz reconciler), W8 (`hold_fader_at` → degrade to an assertion, not a repair-then-refuse ladder), W13, and the two already-dead X1/X2. |

**W6 (the graph-swap duck) is NOT deleted here.** It moves to wave 6, because
`09 §PC-9` binds it to the session graph: *"(a) and (c) land together, or not at
all."*

**Two gaps this plan closes that `01` left open.** `01`'s disposition covers 16
of 18: **W17** (`bass_extension_bench._CamillaFloor`) is in neither list, and
**W18** appears only as "close the bypass." Assign both — W17 becomes a
`session-measurement` claim, W18 a `commissioning` claim that loses its own door.
One grep also owed: chip-AEC commissioning's `prepare_volume()` → `-20.0` /
`restore_volume(original)` may be an uncounted 19th writer (`09 §2`, a lead, not
a finding).

**Do not lose the disclosure discipline.** Whatever `hold_fader_at` becomes keeps
(a) the unconditional proving read, (b) the empty-vs-numeric `observed_db`
discriminator, (c) a positive `result=held` line. Each exists because absence of
a log line was mistaken for absence of a problem — #2198, #2085.

**One interaction to name in the PR body.** Deleting W3 dissolves the *reason*
the 40 dB duck depth is load-bearing: it was chosen to trip
`RECONCILE_DUCK_SKIP_DB` (10 dB) so the cross-process 1 Hz reconciler would leave
the fader alone. That coupling **is** pinned — mutation-verified bidirectionally
(`08 §Probe 6`: 40→5 breaks it, 10→50 breaks it, 40→15 intact) — but **stated
nowhere as an inequality**, so a grep-based reviewer wrongly concludes NOT
PINNED. Update the pin in the same PR, and beware the co-firing noise test
(`test_failed_graph_mutation_restores_the_pre_swap_volume` hardcodes `vol=-58`
and fails on any value change, which can steer a maintainer to "fix the literal,
move on").

**Counted.** 18 → 1 owner / 4 claim kinds. Hardware doors 2 → 1. Confirm
tolerances 3 → 1. Declared-level notions 5 → 1. Four live + two dead writers
deleted. Tests: **4,907 lines the census never counted** —
`test_volume_coordinator.py` (3,210), `test_camilla_controller.py` (1,331),
`test_camilla_ducker.py` (366). *"A plan that schedules the volume collapse
inside a strangler wave will be editing files this census never counted"*
(`10`) — which is why it is its own wave (`00 §7.4`).

**Coordination — this wave works OUTSIDE the census zone, by agreement.** It edits
`volume_coordinator.py`, `camilla.py`'s fader/duck paths, `mux.py`'s writer sites,
`control/volume_ops.py`, `web/sound_setup.py`, `cli/aec_tune.py` and the three
volume suites. The audit program has agreed to widen single-owner area 3 to **"the
volume surface" for the duration of this wave**: ours while it runs, untouched by
them without our acknowledgement, expiring when the wave closes. **Every wave-5 PR
body carries a one-line notice when it lands outside the zone** (§6 R5).

**Verify.** Class-A green; the three volume suites green; then the acceptance run
(§5 row 9) before the wave closes. **Gate:** the 0 dB ceiling is on the closed
clamp list, so 5a and 5c get a **real review**; the rest one pass.
**Rollback:** `git revert`, per-writer — each 5b routing is independent. Per rule
3 a routed writer's old path is deleted in its own PR, not left dual-writing.

#### Wave 5 — CLOSED. The writer ledger, verified at HEAD.

Every row below was checked by grep at the merge HEAD, not carried from the
census. Where the plan's original disposition was wrong, the correction is
stated rather than quietly replaced.

| # | Writer | Final disposition |
|---|---|---|
| W1 | household level | **ROUTED** — `volume_coordinator.py` `declare_household_level_db` |
| W2 | `Ducker` transient duck | **ROUTED** — `camilla.py` `acquire_duck` / `release` |
| W3 | 1 Hz volume reconciler | **KEPT.** Cross-process post-idle-exit recovery, unreplaced. Retires behind #3038. Its `GRAPH_SWAP_DUCK_DB > RECONCILE_DUCK_SKIP_DB` coupling is now pinned *as an inequality*. |
| W4 | `CueDuck` | **ROUTED** — `camilla.py` `acquire_duck` / `release(household_level_db=)` |
| W5 | mux writer sites | **ROUTED via W1.** Verified: `mux.py` holds a `VolumeCoordinator` and writes through it; it has no fader call of its own, so it inherits W1's door. |
| W6 | graph-swap duck | **NOT wave 5** — bound to the session graph, moves to wave 6 (`09 §PC-9`) |
| W7 | crossover-v2 session-measurement write | **NAMED EXCEPTION.** `web/correction_crossover_v2._session_volume_io`'s `_set` (`:1238` at this merge; the symbol is the durable citation, the line is not — wave 3's merges moved it 11 lines while this ledger was in flight). Dies with wave 6's flow; deliberately not routed into a seam that is about to be deleted. |
| W8 | `hold_fader_at`'s repair | **WRITE DELETED.** Prove-and-refuse kept, with all three disclosure items. |
| W9 | *(plan row)* | **NO SURVIVING WRITER FOUND — stated, not assumed.** I could not identify a distinct W9 writer at HEAD, and `web_commissioning.py` contains no fader write at all. The enumerated-set check below is what actually closes this row: no unaccounted writer exists, so whatever W9 named is either already inside one of the named doors or gone. Recorded this way rather than given a mechanism I did not verify. |
| W10 | autolevel ramp | **ROUTED** — one held `SESSION_MEASUREMENT` claim, moved by `relevel` |
| W11 | unresolved-volume recovery | **ROUTED** — `declare_household_level_db` + the lease's own exact→emergency ladder |
| W12 | measurement-session facade | **SPLIT, and the plan's row was wrong.** The autolevel family *is* a facade of W10. The level-match family **writes the fader itself** and routes as itself — one cross-request claim across the ramp, the before-sweep re-assertion, and the restore. |
| W13 | settle-time household return | **ROUTED, not deleted.** See catch five. |
| W14 | measurement volume guard | **ROUTED** — claim acquired at the calibration level, released with `household_level_db=` |
| W15 | commissioning runtime port | **STOPPED, by ruling.** `commissioning_service.py:1336` is not a write — it is the `set_listening_volume_db` field of the `CommissioningRuntimePort` constructor. Since X2 landed, its **sole consumer** is `commissioning_runtime._restore`, beside the one surviving `1e-6` drift gate. Routing it would fire that gate on healthy *recorded-not-written* restores under a higher-ranked claim. The bracket keeps its port. |
| W16 | `/sound/` floor-tone audition | **ROUTED** — `COMMISSIONING` claim, `relevel` on the slider |
| W17 | `bass_extension_bench._CamillaFloor` | **DELETED** — never constructed |
| W18 | `cli/aec_tune` | **ROUTED.** Its *ceiling* bypass had already closed; the remainder was the owner. Declares rather than claims, because `main()` brackets across two `asyncio.run` calls. |
| W19 | chip-AEC `prepare_volume`/`restore_volume` | **ROUTED** — the plan's "uncounted 19th". It calls `aec_tune._camilla_set_volume`, so W18's routing carries it; `aec_commission.main()` gained the registration that makes it reachable. |
| X1 | sweep-lease trio + closed-loop solver | **DELETED** — never ran in production |
| X2 | summed-capture runtime | **DELETED** — no production caller |

**The enumerated-set check, at HEAD — widened past the trailing paren.** This
row used to grep `set_volume_db(`, which structurally cannot see a bare
bound-method reference; one exists, so the check is `grep -rn 'set_volume_db'
jasper/`, and it returns **17 hits**. Thirteen carry no writer: the clamped
door's own definition (`camilla.py:684`) and its three internal callers
(`:741`, `:810`, `:827`); the owner's own binding
(`volume_coordinator.py:2830`, inside the
`install_volume_owner(VolumeOwner(...))` call); and eight prose mentions
(`camilla.py:457`, `:700`, `volume_owner.py:205`, `seat_level_ramp.py:1336`,
`session_seams.py:158`, `correction_setup.py:291`, `cli/aec_tune.py:308`,
`audio_measurement/level_solver.py:32`). Four sites remain:
`volume_coordinator.py:2068` (the coordinator door the owner is *built on*, not
a competing writer), `commissioning_service.py:1336` (**W15**, ruled stopped),
`correction_crossover_v2._session_volume_io` (**W7**, the single named
exception, dying with wave 6), and — **the one the paren-grep cannot see** —
`cli/seat_level.py:413`, binding `set_main_volume_db=cam.set_volume_db`.
That last is a separate CLI process (`jasper-seat-level`) that never calls
`install_volume_owner`, so `volume_owner()` answers `None` there
(`volume_owner.py:799-806`) and there is no owner for it to arbitrate against.
**No unaccounted writer remains — and the claim no longer rests on a grep that
could not find one.**

**Six catches, recorded because each was a census entry that read one way and
behaved another.** W3 (reads deletable, is the only cross-process recovery) ·
W7 (reads routable, dies with its flow) · W12 (reads facade, half of it writes)
· W13 (reads dead, is the only happy-path restore) · W15 (reads writer, is the
bracket) · and **`CrossoverLevelLease.run_level_match`, which this agent
declared dead and was wrong about** — a bare-name grep answers *where an
identifier appears*, never *what object an attribute is called on*, and a
duck-typed `sess: Any` parameter breaks the chain. Only argument-tracing
recovers it.

**Coordination R5 — the volume-surface widening EXPIRES with this wave**, on the
checkable standard adopted 2026-08-26: the enumerated set holds only W7's named
exception, W15's ruled stop, and the owner's own plumbing. The volume surface
returns to the audit program's single-owner area 3.

### Wave 6 — One measurement graph per session. The swap and its duck go together.

**Goal.** Make the owner's loop order affordable: mic moves outside, cheap config
patches inside.

**Prerequisite (`00 §5.3` fix 1, `09 §PC-5`).** Consolidate the two swap
transactions first — `program_playback.play_program` and
`web_commissioning._load_driver_commissioning_config_for_level`, with its **11
restore functions in one module**, are the same transaction written twice, *"the
single loudest signal in this survey that the swap machinery has outgrown its
shape."* Consolidate **into** `jasper.active_speaker.web_commissioning`: that is
free. A **new** module re-homes the owner and trips
`test_commission_tone_single_owner.py`'s hard-coded `OWNER_MODULE` string —
re-point it in the same PR, never delete it (MS-12; the fork it caught, #1950,
survived months).

| PR | What |
|---|---|
| 6a | Consolidate the two swap transactions into `web_commissioning`. |
| 6b | Install one session-scoped graph — role-routed **and** crossover-free **and** tweeter-protected. Satisfies **MS-1 through MS-6 and MS-13** as written in §2; none of them is optional and none of them relaxes at session scope. |
| 6c | Re-point the two graph-emit guards at the session-graph owner and update the two hard-coded counts (PC-3, PC-4). **Same PR as 6b.** |
| 6d | Delete the **measurement-swap** duck. Scope: measurement path only. `/sound/` apply and `/correction/` apply keep theirs — `test_dsp_apply_ducks_both_the_load_and_its_rollback` pins the correction apply *and its rollback*, "the direction its own docstring calls the loud one" (PC-1). **Not an owner gate — this is design** (see below); its evidence step is the no-pop check. |
| 6e | Claim the free deletion: `held_target_db` (added by #2929/#2936 specifically because the measurement swap's release clamped to the household level) becomes dead on the measurement path. Claim it explicitly rather than orphan it. |
| 6f | `patch_config` stops paying the graph-swap duck. Its one production caller is a per-speaker balance trim, where a 40 dB / 0.45 s fade is plainly wrong. |

**Counted — measured, not derived** (`08 §Test 2`): per swapping stimulus,
**Δ1 ≈ 489 ms + Δ2 ≈ 454 ms ≈ 0.94 s** of pure duck ramp, against a **64 ms**
fader proof on the swap-free branch in the same round with session, hardware and
fader held constant. Across the two baseline rounds' **14 swapping stimuli that
is ~13 s of pure ramp**, and it multiplies by every angle and every retry.
Per stimulus this deletes **2 config swaps, 2 ducks, ≥5 CamillaDSP round-trips**,
the filter-state transient (a full reload resets biquad state), and the
rate-adjust control-loop restart. Swapping capture phases **3 → 0**; swaps per
session **2**.

**The duck is design, not a decision — owner-ruled 2026-08-25:** *"I agree with
the deleting the duck complexity. That sounds exactly right."* The whole
volume-dance mechanism class is **plumbing the LLM never sees**. The new engine
changes configs *without* the dance; a simple pipeline-health check may remain;
the gymnastics are deleted. So this is not an open gate and the plan does not ask
for a nod. What it does ask for is **one piece of evidence** — the no-pop check (**sanctioned S11 validation act 2**)
(swap the graph inside an open measurement window with the fader parked and a
recorder running, and listen). It ran — `cutover-briefs-acceptance.md` §4 owns
the result. Pass → the duck goes on the
measurement path. Fail → the session-scoped graph still fires it once per session
instead of twice per stimulus, which is most of the win, and the health check
absorbs the rest. Either way the scope is unchanged: **measurement-swap duck
only.**

**Three tests get DELETED, not fixed — say so in the PR body (PC-11).**
`test_camilla_volume_ramp_default.py` (63 lines, whole file — it fails at
*import*, not at an assert); the exact-set seam assertion in
`test_crossover_v2_conductor.py:6820`; and the release-reference block of
`test_crossover_v2_measurement_volume_drift.py`. **Keep the
fader-proven-per-stimulus half of that last file** — it is MS-14, the
excitation-safety ledger, not the duck. Decide deliberately about the collateral:
`test_no_camilla_config_generator_emits_volume_ramp_time` is the only thing
keeping any generator from emitting `volume_ramp_time`, and its stated
justification dies with the duck.

**Verify.** If any `rust/jasper-fanin` change has landed since, re-run `08`'s
five-case stereo tap on the box **before** this wave executes (`11`; **sanctioned S11
validation act 3**). Then the
acceptance run in §5. **Gate:** the duck is **not** on the closed clamp list, but
it carries a hearing-safety claim in its own test docstring — *"a hearing-safety
fade across a graph swap that can step the graph's own gain by tens of dB."* So
this wave's PRs get a **real review**, and the no-pop check is the evidence that
licenses the deletion. This is the one place the plan deliberately spends more
ceremony than the directive requires. **Rollback:** `git revert`. 6b's session
flag is a **PROOF BRACKET, not a fallback** — it exists only until acceptance
row 9 passes, and then the per-stimulus swap path **and the flag itself** are
deleted in this same wave. Per rule 3 there is no lingering fallback and no
coexistence window; the old swap machinery dies whole.

### Wave 7 — Doctrine, docs, guards.

| PR | What | Counted |
|---|---|---|
| 7a0 | **Rewrite the doctrine's §1 loop AND its §2 motto into plain words.** The six-step loop (Measure · Propose · Run · Collect · Recommend · Confirm) becomes the owner's cycle: **measure → analyze → recommend → loop → save.** And §2's authority model stops speaking in slogan: *"predictions propose, measurements dispose"* → **the LLM recommends; the measurement decides.** The sense survives exactly; the jargon dies. Per the owner: *"what does that mean? …That is a stupid thing, but let's turn it to recommend and keep it simple language that anyone would understand."* Then sweep the tree for the retired step names and the retired motto used as going-forward language — AGENTS.md's doctrine pointer, the prescriber CLI's prose, `docs/measurement-loop-doctrine.md`'s own §2/§3, and any module or constant that took its name from a step. Per `00 §R4` this is a **deletion from an enumerated set**, the class the sweeps are structurally blind to, so sweep by subject and read the block around every edit. **And write ruling S10 into the doctrine as measurement-zone law:** outside the §4 clamps, an unproven or stale fact **discloses loudly and never stops the work**. One sentence each way against drift: the doctrine says *the repo-wide form of this rule lives in the governance charter*, and the charter says *the measurement zone's form lives in the doctrine*. Neither restates the other's scope — that is the charter's own Docs default, applied to the one principle both files need. | rewrite |
| 7a | **Name the integrity class** in the doctrine's §4 (or a new §4a — the doctrine's numbering, not this plan's), with the "would measuring again fix it?" test quoted from the #2087 ruling, its six sub-kinds, and its three rules. **Name the STOP-RELAXER pattern** — three implementations already, and it is how a mostly-right stop gets narrowed. *(It is no longer #2935's fix: a relaxer lets a block THROUGH, and ruling S10 DELETES that block outright — see 7j. The pattern still earns its name on its own three implementations.)* **Carry S10 itself** as measurement-zone law: outside the clamps, an unproven or stale fact discloses loudly and never stops the work. | ADD |
| 7b | **Delete the deviation table** (9 rows, 8 closed, 1 retained), keeping row (e) `BOOST_ROUTE_UNAVAILABLE` inline as the one live retention — and close `04`'s risk by the other route: **make §4's closed clamp list positively complete**. A reader who can read "here are the 5 clamps and their ~112 enforcement families" never needs a list of what stopped being one. **Settled by S9 — no gate; execute it.** Row (e) `BOOST_ROUTE_UNAVAILABLE` survives inline as a normal ruling sentence, not a table row. | doctrine 255 → ≈150 |
| 7c | Demote the 4 nanny survivors: N1 `ALIGNMENT_CONFIDENCE_TRUST_FLOOR = 0.6` (confidence branch only), N2 `REASON_VERIFY_DETERMINISTIC_MISMATCH` (terminality only — "the clearest §3 violation in the tree"), N3 `INSUFFICIENT_POSITIONAL_EVIDENCE` + `BOOST_DIP_NOT_STABLE`, N4 `BOOST_IN_CROSSOVER_OVERLAP`. | 4 / 5 slugs |
| 7d | Two doctrine edits `04` raises that this plan adopts: state **which registry codes can take a graph off the speaker** (`TEMPLATE_HARD_STOP`'s "0 retries" collides with §4's "hard stop" in the same file), and say whether a published **slope** is inside §4. | ADD |
| 7e | Docs: 126 files / 70,039 lines → **three authored docs at ≈1,550 lines** (doctrine ≈150 · one merged operational runbook ≤600 replacing `llm-operator-runbook.md` + the live spine of `HANDOFF-crossover-measurement-v2.md` · one fresh engine design doc ≤800) **plus `testing-tooling.md` as the tool index at whatever size it earns**. Everything else → `docs/historical/`, tagged once, removed from `doc-map.toml` so the routing bot stops sending readers to 26 documents for one subsystem. Note this is mostly **archival, not deletion** — what actually leaves the repo is 7f, and only 7f is in the net-lines table. | see 7f |
| 7f | The mechanical doc cuts: two HANDOFF hybrids lose their appendices (**−8,043, zero information lost**); the five-file linearization plan family collapses to one archived decision record (**−4,527, −5 files, −5 doc-map rows**); six already-`Status: historical` docs move. Fence `docs/research/`, `bass-extension-waves/`, `correction-ux-wave3/` (78 files / 19,460 lines) off the maintenance rules — they are primary sources and are *supposed* to be frozen. | −12,570 |
| 7g | **Move `docs/calibration-agent/` (12 files / 1,356 lines) out of `docs/` entirely.** It is runtime product input, resolved at run time from three search roots by `calibration_agent/tools.py`. Its residence in `docs/` is what forces every doc rule to make an exception for it. `07` calls this the single highest-leverage item in its section — and it is a code change, not a doc change. | code |
| 7h | Guards **7,436 → ~600**. Delete `MAX_LINES_BY_PATH` and its 1,439-line comment block (`test_lint_contracts.py` is 2,159 lines, **87.9% comments**, grew 61 → 2,159 in ten weeks, **7 of 8 ceilings at exactly zero slack**, **1 confirmed catch in 60 commits — outcome: ceilings raised**, and 13 of the last 60 commits were forced to edit it). Delete `test_doc_staleness_sweep_20260604.py` (167), `test_docs_handoff_freshness.py` (180), `test_crossover_v2_measurement_doc_pins.py` (336 lines and a 4-path AST resolver to pin 2 sentences — delete the restatement instead, which is the doc's own instruction). **Keep** `test_package_enumeration_contract.py`, `test_measurement_integrity_floor_contracts.py`, the two code-vocabulary tests, and everything pinning the 5 clamps. Replace the ratchet with **one un-ratcheted assertion at a round number nobody edits** — fires once, cannot be paid off with a paragraph. **RE-BASELINE — DONE (§6 R9b).** The governance-reset branch already deleted `test_agents_md_toc.py` (108) and `test_doc_staleness_sweep_20260604.py` (167) — **275 lines banked**, so this row is **−6,561**, not −6,836. It did **NOT** touch `test_lint_contracts.py`, `test_docs_handoff_freshness.py`, or `test_crossover_v2_measurement_doc_pins.py`; all three remain ours to delete. *(One clause for the executing session: `07`'s 22-test guard slice was the tuning slice, and the ToC test was not separately enumerated in it — confirm the 108 sits inside the 7,436 before booking the full 275.)* | **−6,561** |
| 7j | **Demote the #2935 topology-staleness block** (ruling S10's worked example, and this program paid for it TWICE this week). Today, entering `driver_style` — **metadata** — rotates `topology_config_fingerprint`, the box goes `blocked` / `active_baseline_topology_changed`, and **a v2 measure session refuses to open** even though the edit provably does not affect the compiled graph for that role. After this: **playback continues on the applied graph, measuring continues**, and the fact surfaces as **one loud `event=` plus a doctor line** — *"topology changed since the applied baseline; re-mint when convenient."* **The narrow carve, and it is the only one:** a **DECLARED-CAP change that makes the currently-applied graph exceed the new limit** is clamp territory and may gate. **Metadata never does.** **Verify on the box that a metadata edit no longer blocks a measure session, and that the doctor line appears** — **sanctioned S11 validation act 5**. | a block becomes a disclosure |
| 7k | **Small docs — the guide's stale Adoptions header.** `docs/crossover-design-guide-deep-research-2026-08-19.md` claims four adoptions in the present tense and **none of the four exists in the tree** — the measured-directivity bounds, the three-source precedence, the DI-continuity objective term, and the "slope-aware distortion-informed tweeter floor" were all to live in `candidate_space.py`, deleted with `search.py` and `objective.py` at `a31f1fa24` (#2832). `tuning-master-plan.md` and `tuning-operator-runbook.md` both record the deletion correctly; this header is the sole stale claim, and R-4 must not import it. **Already dispatched as a separate small docs PR — do not re-do it**; listed so the plan does not double-book the fix. | `13 §H8` |

**Wave 7i is DROPPED, and the reason is self-demonstrating.** It proposed adding
`07`'s rule to AGENTS.md — *a doc may state a fact once; if a second doc needs it,
the second doc links.* The governance charter's **Docs default** already owns that
substance: *do not restate here, in README, or in code what another file owns.*
Re-adding it would be a second file restating what the charter owns — the exact
thing the rule forbids. So the rule is kept by **obeying** it, not by writing it
down again. (Audit-program reconciliation, 2026-08-25, via the owner.)

**`test_lint_contracts.py` has exactly one owner: this refactor** — and the audit
program has now **ceded it in writing**, along with `test_docs_handoff_freshness.py`
(§6 R5). The
audit agent does not touch that file. Two hands in one 2,159-line file guarantee
conflicts, and 7 of its 8 ceilings are this zone's files.

**Verify.** `bash scripts/tense-grep.sh --all` against a baseline captured
*before* the cut — and treat it as a floor, never a proof. This wave deletes many
members of enumerated sets (refusal slugs, record types, fader writers, doc
files), which is the class the sweeps are structurally blind to (`00 §R4`). Sweep
by **subject** as well, and read the whole block around every edit. **Gate:**
docs and guards get author plus a sanity look — except 7b, which **is** the clamp
list and gets a real review. **Rollback:** per-PR revert; `docs/historical/` moves
are `git mv`.

#### Wave 7 — CLOSED. The doc ledger, verified at HEAD.

Every number below was measured at the merge HEAD, not carried from the row that
booked it. Two rows did not reach their line target; in both the target lost to
content the wave itself had ruled non-negotiable, and that is recorded as the
outcome rather than the number quietly restated.

| Row | Landed | Where |
|---|---|---|
| 7a0 | **DONE** | `1748db0e8` (#3056) |
| 7a + 7d | **DONE** | `625bd75e0` (#3058) — one commit, both rows |
| 7b | **DONE on substance, NET-ZERO on lines** | `95b27355b` (#3060). The table went and §4's clamp list became positively complete. Its `255 → ≈150` count was never this row's to hit: 7b landed **361 → 361**, a near-even rewrite |
| 7c | **DONE** | `5217b218e` (#3085) — three demoted, the fourth refuted |
| 7e | **DONE, four parts** | `ae305dea2` (#3083) · `0e0da9f97` (#3086) · `031bfd360` (#3113) · `39233aaab` (#3131) |
| 7f | **DONE, two parts** | `b25216fff` (#2979) · `0e0da9f97` (#3086) |
| 7g | **DONE** | `72da802b6` (#2981) — `docs/calibration-agent/` is gone from `docs/` |
| 7h | **DONE on outcome, NOT on mechanism** | `359acd9d1` (#2977) — see below |
| 7j | **DONE** | `b56ea4257` (#3006) |
| 7k | **DONE** | `772f8c8cb` (#2943) — already flagged here as dispatched separately |

**Attribution warning for anyone re-checking this table.** 7f (part), 7g, 7h and
7k landed **before the "wave 7" commit-subject convention started** — they are
#2943, #2977, #2979 and #2981, all below the #3056 mark. A ledger built by
grepping subjects for `wave 7` under-reports by four rows.

**The doctrine did not reach ≈150, and its own floor is why.**
`docs/measurement-loop-doctrine.md` is **333** lines. §4's body is **129** and
its nested `### 4a` is **65** — **194** lines that are the closed clamp list plus
the integrity class, i.e. exactly the content ruling **S9** made *positively
complete*. 194 alone exceeds 150, so the target became unreachable the moment S9
chose completeness over brevity. **Content outranked the number; the number is
the one that gives.** Two corrections a re-checker will otherwise trip on: the
trim ran **361 → 333** (`031bfd360`, #3113, wave 7e-2), not 255 → 333 — waves
7a0/7a/7d grew the file by 106 lines first — and `031bfd360`'s own message says
"361 → 329" and quotes 63/192, all **pre-restoration** figures from before the
same PR's later commit. HEAD measures 333 / 65 / 194. Note also that §4a is a
subsection *inside* §4, so "129 + 65" partitions §4's 194; it is not two siblings
summed.

**The runbook did not reach ≤600, and the split is why.**
`docs/tuning-operator-runbook.md` is **1037** lines. Two sections — *The capture
flow* (240) and *Reading the per-feature evidence* (201) — are **441** lines,
42.5% of the file, and both are unambiguously operational. Everything else sums
to **596**, so reaching 600 means deleting essentially one of those two whole.
The only place that content could go is the engine design doc, which would
**invert the very split 7e exists to create**. The runbook keeps it.

**The three authored docs, counted at HEAD:** doctrine **333** + runbook **1037**
+ `docs/crossover-v2-engine-design.md` **565** = **1935**, against the ≈1,550
target. The engine design doc is the row that beat its bar, by 235 lines. One
correction to 7e's own wording: `llm-operator-runbook.md` (673) and
`HANDOFF-crossover-measurement-v2.md` (1,288) were **deleted outright**, not
archived — only a distillation reached `docs/historical/`.

**7h's outcome landed; its instruction did not.** `test_lint_contracts.py` is
**288** lines at HEAD, down from **2,164** (the row says 2,159 — off by five);
the last 302 lines of that drop were the noqa ratchet's own dated ledger, cut
once the line-ceiling half had already been converted. But
**`MAX_LINES_BY_PATH` was not deleted.** It was *repurposed* into the row's
own prescribed replacement: an eight-entry un-ratcheted ceiling table at round
numbers, carrying its own removal condition. A ledger claiming the constant is
gone is checkable and false. The four named guard deletions all did land.

**One backlog item removed as false.** The runbook's bare `(invariant 2)` — at
**:76**, not :75 — **RESOLVES**: `docs/tuning-master-plan.md`'s *Product
invariants*, item 2 (*"Executor, not hunter"*), which the master plan cites by
the same number at its own `:300`. Recorded because the failure mode is
reproducible: **two numbered invariant lists exist in this doc set** — the master
plan's Product invariants and this plan's MS-1…MS-17 — and resolving the runbook
against MS-2 (*Both ends move together*) yields a confident false negative.

### Wave 8 — Census-zone prose and test discipline.

**Goal.** Take the work the audit priced and handed over.

The deep audit's own §6 owner-decision 2 — its numbering, not this plan's gates —
reads verbatim: *"**Tuning-stack participation** —
apply Waves 3–4 discipline inside the census zone (~55K available; coordinate
with the other agent; the `commissioning_capture_producer` orphan + its doc drift
is theirs to take)."* Accepted. The orphan is wave 4g; this wave is the rest.

**Counted.** **~30K prose** + **~15–20K test docstrings / fixture de-duplication
as a RESIDUAL** — revised down from the audit's ~25K by ruling S7, because the
function-grained deletions in waves 4–7 take their own docstrings with them. The
plan books the conservative end (15K). Zone context: 68.9K prose lines at a 0.61 prose:code ratio and
244K lines of tests (2.05× its source; crossover_v2 tests 2.5×). **Prose +
test-altitude trims only, no behaviour changes.** Two things the audit's own
verifier **refuted** and this plan does not retry: the blanket prose sweep
(*"dated citations are the owner's documented house style, and most dense prose
is genuine WHY-constraint documentation"*), and *"just delete the tuning stack's
journeys"* (the flows are live and cross-wired). Category-scoped, by hand:
superseded-value changelogs, self-labelled archaeology blocks, incident
narratives for bugs the constant does not fix.

**Sequencing — this wave runs LAST, and ruling S7 is why.** Trim docstrings only
**after the deletion bracket resolves.** Editing prose in a file that is about to
die is wasted motion, and S7's function-grained triage is what decides which
files those are. So waves 4–7 delete first; wave 8 trims what is left standing.

**The docstring rule, from S7 — the sharpest line in the whole deletion story:**

> **A docstring line may be deleted only if every constant it justifies either
> leaves with it or keeps its derivation.**

Hand-derived expected values **are the audit trail**. Cut narrative; never cut
derivation. A test that says *"expected −6.02 dB because …"* and shows the
arithmetic is not prose, it is the proof that the number was not guessed.

**Rulings get ADRs — and this is also how `00 §R5` gets satisfied.** §0's rule 1
says *extract the rulings before the code moves*; S7 supplies the mechanism:

- A design ruling that **still binds the new engine** but lives only in a test
  docstring becomes an ADR in **`docs/adr/`**, and the docstring shrinks to
  `See ADR-NNNN`. **Our range is ADR-0002 – ADR-0099**; the audit program takes
  0100+. ADR-0001 is the governance reset's operating-model record (§6 R9c).
- A ruling the refactor **supersedes** is deleted outright. **Git history is the
  archive** — that is the same call ruling G1 makes about the deviation table's
  demotion record.

`docs/adr/` does **not** break wave 7e's minimal-doc-set ruling. It is an
append-only record class, like `docs/historical/` — not one of the three authored
live docs, not routed through `doc-map.toml`, and never edited after it lands.
The four rulings `00 §R5` names — the #2087 discriminator, the PR-L4 review B1
frame argument, `_duck_release_target_db`'s three design constraints, the
`fader_db` asymmetric-record-point reasoning — are the first ADRs, and they are
written **before** the code that carries them moves, not after.

> **ALREADY CLOSED — the four landed ahead of this wave, exactly as instructed.**
> `docs/adr/0002-measure-again-discriminator.md` ·
> `0003-prediction-gate-frame.md` · `0004-duck-release-algebra-and-reference.md`
> · `0005-fader-bound-asymmetric-record-point.md`. Our range now runs to
> **ADR-0019**. The mechanism proved itself on the way: wave 6e deleted
> `_duck_release_target_db`'s held branch and **ADR-0004's bound-never-inverts
> invariant did not die with it** — it lives in the ranked owner, pinned there.
> That is what "extract the rulings before the code moves" was for. Nothing owed
> here; do not re-write these.

**Verify.** Class-A green; class-B and class-C suites green or deleted on
purpose. **Gate:** mechanical — author plus a sanity look. **Rollback:** per-PR.

---

## 4. What the owner settled — twelve rulings, no open gates

### Settled — owner rulings, 2026-08-25

These replace the corresponding open decisions the inventory raised. Quoted so a
fresh session does not re-litigate them.

| # | Settled | The ruling |
|---|---|---|
| **S1** | **Vocabulary: `measure` · `analyze` · `recommend` · `save`** | One vocabulary, not two layers — the engine's verbs **are** the loop's language, in words anyone would understand. *"Measuring is measuring"*: baseline, re-measure and candidate-check are one parameterized verb, one implementation, no duplication. *"Propose and prescribe and recommend are the same thing"* — the final recommendation *"is just now got more information."* **The plain word wins.** The owner also retired the slogan that had been the argument for calling it `propose`: *"'predictions propose, measurements dispose' — what does that mean? …That is a stupid thing, but let's turn it to recommend and keep it simple language that anyone would understand."* The motto's **sense** is kept and said plainly — *the LLM recommends; the measurement decides* — and wave 7a0 rewrites the doctrine's §2 accordingly. Run and Collect are *"just back to measure again."* Saving *"is simple."* **This overrules `00 §3.5`'s six-steps-plus-Run recommendation, which this draft carried before the ruling.** The engineering fact under that recommendation survives, restated: the play transaction is a **named internal module inside `measure`** with its own contract — a real code boundary, not a vocabulary item, because pipeline mechanics are *"just the mechanics of how we execute the verbs… the LLM doesn't really care about"* them. Wave 7a0 rewrites the doctrine's §1 loop to **measure → analyze → recommend → loop → save**. |
| **S2** | **#2202: FIX it.** | *"2202 does seem like it should be fixed."* The commissioning lane is repaired, not routed around — which flips wave 4g from the deletion path to the **producer** path, and withdraws a −2,089-line deletion this plan had booked. |
| **S3** | **`DriverResponse`: bank phase too.** | *"When we take a measurement, it should just be easy to get all the information we need… right now, let's just save the information."* Overrides fragment `02`'s *"magnitude now, phase behind a flag."* **The counter-argument stays on the record, CORRECTED by `13 §H3-a` — the draft version of this line was too strong.** Not *"nothing reads phase"*: **phase is computed and consumed in-process every round** (`DriverResponse.complex_tf` is a full complex transfer function, read by `crossover_v2/intervention.py`, `forward_model.driver_plants` and `spatial.lateral_pose_curve`; `compose_linearized_prediction` exists *because* magnitude-only measured ~2.0 dB worse). **What is missing is PERSISTENCE — and that is exactly what 4a fixes**: `complex_tf` has zero serializers, so every re-analysis re-derives phase from WAVs, and the forward model can never run from the bank. The real residual caveat is narrower: REW exports emit a literal `0.000000` phase column, and mic calibration is magnitude-only, so *absolute* phase carries the mic's own uncorrected response — common-mode and self-cancelling for relative cross-driver work inside one capture. *(Correcting this in place matters: a fresh session reading the old parenthetical would conclude phase is unused and might drop 4a as speculative.)* Size sanity: the cloud path already banks 89 points per position at 1/12 octave. **Addendum:** measurement management — sessions, browsing, deletion structure — is explicitly **future scope**, not this plan. |
| **S4** | **Net-negative accounting.** | End-state deletion is the bar; temporary adds during a wave are fine. Hard rule: *"we're not investing in systems we're going to be deleting"* — no throwaway scaffolding. (Wave 1's test twin passes: it is permanent infrastructure for the permanent engine.) |
| **S5** | **The old path dies immediately.** | *"The old path should die right after we've got the new one in… fallbacks aren't a thing. We're not going to have duplication… deleting old systems whole hog."* Build new → prove → delete old, inside one wave. Every "old route" and "session flag" in §3 is a **proof bracket**, never a fallback. |
| **S6** | **The duck: dissolved, not decided.** | The whole volume-dance mechanism class is plumbing the LLM never sees. The engine changes configs without the dance; a simple pipeline-health check may remain; the gymnastics go. Presented in wave 6 as **design**, with the no-pop check as its evidence step and the real-review gate retained. |
| **S7** | **Deletion aggressiveness: the unit is the TEST FUNCTION, not the file.** *(Source: the audit agent's consult, 2026-08-25, via the owner.)* | This **supersedes the file-grained question** that stood here as gate G3 — `10`'s A–E classes are a map, not a verdict, and a mixed file is the normal case. **Default-aggressive is licensed for dying happy-path choreography ONLY**, with three exceptions that are class B in costume: FAILURE-BRANCH pins (the proving ground exercises none of them), DEADNESS-ENFORCING tests (die in the same PR as their subject, never before), and CROSS-PROCESS / cross-language pins (grep the subject's literals repo-wide first). The mechanical triage pass, the never-window-sample rule, and the deletion checklist are operational and live in **§3, "The deletion rules"**, because they bind every deleting wave — not just wave 8. Two consequences priced elsewhere: the docstring residual drops to **~15–20K** (S7's deletions take their docstrings for free, which also **retires this plan's double-count caveat**), and the invariant→pin table becomes **wave 0 PR 0d**. |
| **S8** | **"Level-matched" means matched ACOUSTIC OUTPUT THROUGH THE HANDOVER REGION.** *(Source: owner-commissioned prior-art research, 2026-08-25.)* | After the target filters are applied, the two driver traces are **equal at Fc and each sits −6 dB against the summed target** — the Linkwitz-Riley unity condition; Rane/Bohn's *"amplitude response of each is −6 dB at crossover"*; McCarthy's equal-level acoustic-crossover definition. **Passband-average sensitivity is NOT the level fact.** It is the *starting estimate* that sizes the horn's fixed attenuation, and on a horn with a sloped response the two conventions **legitimately disagree by many dB**. Consequences: `solve_branch_trims` (linear-frequency power mean over mirrored ±1-octave halves about Fc) **is** the consensus statistic and becomes the level fact; `driver_core_level_db` is demoted, kept, and its delta disclosed — the research's own instruction being to *"surface this discrepancy to the user rather than hiding it."* **This explains the 8.6 dB dispute rather than fixing it:** the two owner comments two rounds apart that pointed opposite ways on the same numeric pair were each right about a different quantity, and `05`'s "defect 1" was never a defect in either estimator — it was a comparator asking a question neither one answered. Implemented as wave 4k; the campaign instrument it unlocks is in §5. |
| **S9** | **The doctrine deviation table: DELETED.** Owner, verbatim: *"delete it."* | The nine-row table goes. **§4's clamp list becomes positively complete** — that is the substitute, and it is the part that must actually get written: a reader who can read the 5 clamps and their ~112 enforcement families never needs a list of what stopped being one. Row (e) `BOOST_ROUTE_UNAVAILABLE` **survives inline as a normal ruling sentence**, not as a table row — it is a live retention, so it reads as doctrine, not as archaeology. The demotion *record* lives in git history. `04`'s counter-proposal (add rows j, k, l and the camilla slope arm) is answered rather than overruled: those four demotions are real and already landed, and a positively-complete clamp list makes them unnecessary to track. **Wave 7b's gate is clear.** |
| **S10** | **Attestation never gates OPERATION — only the clamps do.** *(Owner ruling, 2026-08-25.)* | Verbatim: *"things should just keep working as we are. And if something I did upstream breaks it, then we should just loudly complain… there should be a clear issue in the doctor… we shouldn't stop stuff from working because it hasn't been proven yet… when someone else downloads the software and they have that hardware that I've already tested… they shouldn't have to recommission it. It should just work."* Three consequences. **(1) Last-known-good keeps working.** Staleness and unproven-ness become a **LOUD disclosure** — a doctor line, an `event=`, a UI hint — **never a stop.** **(2) Proof travels with the PROJECT, not the box.** Hardware the project has commissioned once ships as known-good; per-box re-proving is reserved for genuinely novel hardware, and even there it **discloses and degrades** rather than parks, wherever the clamps permit. **(3) The distinction this plan already drew becomes law: refusing to WORK dies; refusing to CLAIM stays.** The integrity class banks nothing it cannot prove — and never stops the speaker playing or the session measuring again. **MS-14 is the canonical survivor**: a fader that cannot be proven refuses to BANK the capture; it does not refuse to play the stimulus, and it does not refuse to try again. Outside the §4 clamps, that is the only shape a refusal may take. |
| **S11** | **Sequencing: finish the refactor FIRST. Hardware use during it is ENGINEERING VALIDATION ONLY.** *(Owner ruling, 2026-08-25.)* | Verbatim: *"I don't think there's any point in continuing to try to measure and test the speaker until all of this stuff is done, unless you're just trying to validate that your code is working as you expect on actual hardware, in which case that's fine… the cleanup and refactor is the most important thing. We'll get that done and then we'll start measuring on hardware once everything's landed to flush out issues."* **The licence is CLOSED, not open-ended — six sanctioned items, and only these six:** **(1)** acceptance row 9, the baseline reproduction — *an instrument-validation act, not a measurement campaign*; **(2)** wave 6's no-pop check; **(3)** the 5-case stereo-tap re-verify after a fan-in change; **(4)** the #2202 one-hour scoping; **(5)** 7j's demotion verification; **(6) The commissioning producer proof** — *owner-ratified 2026-08-26 (in-chat), text drafted in `cutover-briefs-acceptance.md` §3*. *One* commissioning run on jts3, for the sole purpose of proving that the re-armed summed-capture producer writes the evidence its readers are waiting for. **It is an instrument-validation act, not a commissioning of the speaker** — the run's product is a receipt and a `complete.json`, never a tuning change. **Scope, and nothing outside it.** One speaker (jts3), one phone through the capture relay, one region. Permitted: capture, admit, promote, publish the `AdmittedCaptureProof` envelope, emit the `protected → measured` transition, write `runs/{run_id}/complete.json`, mint the eligibility receipt. **Not permitted: applying any candidate the run produces, any EQ or crossover change to the speaker's sound, a second region, a second speaker, or a retry loop.** A failed run ends and is reported; it is not re-attempted the same night. **Evidence required — five things, and the act closes only when all five read true.** (a) `capture_post_apply`'s proof is **published**, and `_reopen_capture` parses it with `AdmittedCaptureProof.from_mapping` and reaches every child through `reopen_artifact(identity)` — never by reconstructing a path. (b) A production `CommissioningTransition` emits `to_state="measured"`, which no site in the tree does today. (c) `publish_complete_commissioning_evidence` runs from production, and `commissioning_host.status()` **polls without raising** — the warning 4h says travels. (d) `read_commissioning_room_authority` returns something other than a denial, and the code it returns is named. (e) The bench ends with the standing park. **Bound.** One run. If the producer needs a second attempt, that is a second act and comes back for a second sanction. **The receipt RECORDS and never FORBIDS** (ruling S10): a receipt that cannot be produced leaves the lane working and says so loudly, so a failed act blocks nothing — it costs an evening and a report. **This act does not open acceptance row 10** and grants no tuning licence; the five acts plus this one remain the whole list. **NO tuning. NO candidate campaigns. NO commissioning beyond act 6's single bounded run. NO EQ or crossover changes to the speaker's sound** until the plan's acceptance closes. Then — and only then — the campaign (acceptance row 10, the owner's linearization bar). **Anything hardware-shaped that is not on this list gets ADDED to it explicitly before it runs, never assumed onto it.** *Operational form (the bench discipline this program already follows):* **jts3 stays parked between validation acts**, and every act ends with the standing park — **nothing playing, fader at the household value, spool clean** — exactly as `captures/postfix-baseline-2026-08`'s end-state probe records it. *[Amended 2026-08-28: acts 4 and 6 defer with the relay park — see [ADR-0188](adr/0188-wired-first-measurement-relay-parked.md).]* *[Amended 2026-08-29: act 1 (row 9) is superseded — the baseline-reproduction bar is retired and its mechanics reopen as the campaign's day-1 preflight; act 5 is unchanged, now read as part of that preflight — see [ADR-0192](adr/0192-the-campaign-is-the-validation.md).]* |
| **S12** | **Mic-only capability holes are STUBBED, not omitted.** *(Owner ruling, 2026-08-25.)* | Verbatim: *"If there's a hole in our existing capability that the plan points out — obviously not stuff that involves new hardware, like measuring resistance — but for just microphone measurements… we would just stub it for the time being."* **The `measure` / `analyze` parameter surface is built COMPLETE for every mic-only regime this plan identifies, from day one** — `regime=near_field`, `position_axis=vertical`, distortion-vs-level, `polarity=inverted`. An unimplemented regime is a **LOUD STUB**: invoking it returns a named not-implemented disclosure saying exactly what is missing — *"near-field splice not implemented; capture banked, splice pending R-3"* — in the same shape as the analyze default's missing-input wording (§1). **Never a silent skip, and the API shape never changes when the capability lands**, so a preset may name a stubbed regime today and simply start working later. This is **S10's disclosure discipline turned on our own gaps**: the engine admits what it cannot do instead of pretending the parameter does not exist. **New-HARDWARE regimes stay OUT of the parameter surface** — impedance (R-6) gets no stub until the owner's hardware decision, because a stub for hardware that may never exist is exactly the speculative flexibility the charter forbids. |

### Settled — owner ratifications, 2026-08-26

The owner ratified every pending recommendation in chat on 2026-08-26. None
reopens S1–S12; each closes a question that was left waiting on him.

| Question | Ruling |
|---|---|
| The S11 amendment for the producer build (`cutover-briefs-acceptance.md` §3) | **ADOPTED** as S11 act 6, above. Its drafted text is quoted there; §3's banner no longer drafts. |
| D12 / `REFACTOR-CUTOVER-2026-08.md` §6.2 — is `RecordStore` **the** durable-write seam or **a** durable-write seam? | **FOLD.** The five `V2FlowSeams` publishers fold into `RecordStore`; fail-soft stays at the caller, on `_hand_to_retention`'s shape. W1-a's scope gate lifts. |
| Appendix A — grow the charter with a greppable-disclosure default, or drop the attribution? | **DROP the attribution; do not grow the charter.** Executed as a comments-only sweep in a sibling PR; the closed list is not touched. |
| Archive `gating-v2-plan.md` and `room-correction-regime-plan.md`? | **No — both stay STANDING.** Archival declined. |
| `derive_position_curves.py` | **Deleted** from the owner's archive. |
| The R-6 impedance jig | **Deferred to the bench evening.** S12's carve holds: no stub until the hardware decision lands. |

One correction rides with these rather than being silently applied: the plan's
*"fourth retention site"* premise is refuted and is now recorded in
`REFACTOR-CUTOVER-2026-08.md` §0's ledger — the lift is **three** sites.

### Still open — none

**No open gates. The plan is final.**

Every decision the inventory escalated has been answered by the owner on
2026-08-25 and recorded as S1–S12 above. Nothing in this plan waits on a ruling;
what remains is execution, and the evidence still owed is named in §6 rather than
left as a decision in disguise — the no-pop check, the #2202 scoping hour, and
the class-B verification pass are measurements to take, not questions to ask.

### One decision arrived WITH new scope — and it is not a reopened gate

Said plainly, because the heading above must stay true. S1–S12 settled **this
plan's shape**, and none of them reopens. But the audit-program reconciliation
(2026-08-25) **assigned this plan a package it did not previously own**, and that
package came with its own owner decision attached:

> **#1738 — wire `jasper/bass_extension/` up, or delete it.** ~4,600 lines plus
> 715 lines of test. The audit program's verifier supplies the negative proof
> (deadness structurally enforced by the package's own tests); the owner supplies
> the call. One PR either way, with the deadness-enforcing tests dying in the same
> PR per S7.

This is a **new scope item carrying a decision**, not a gate this plan left open —
the distinction matters, because a fresh session reading "no open gates" and then
finding an owner decision three sections later would rightly distrust the rest.
Detail sits beside wave 4.

---

## 5. Acceptance, counted

The plan is DONE when every row below reads true.

*One distinction, because ruling S10 makes it easy to blur:* **these are development
gates on merging a wave, not runtime gates on the speaker.** S10 governs what the
product does when a fact is unproven — it keeps working and complains loudly. It says
nothing about whether *we* merge a wave before its evidence lands, and we do not.

| # | Criterion | From | To |
|---|---|---|---|
| 1 | **Fader writers** | 18 production-reachable (20 in tree) | **1 owner, 4 claim kinds.** Hardware doors 2 → 1. Confirm tolerances 3 → 1. Declared-level notions 5 → 1. |
| 2 | **Swapping capture phases** | 3 of 7 require a swap | **0.** Config swaps per stimulus 2 → 0; per session, 2. Duck ramp per swapping stimulus ~0.94 s → 0. |
| 3 | **Capture record shapes** | **4** same-fact duplications | **1** unified record, five blocks, place as a field, `DriverResponse` banked. 24 orphan sites cleared; 2 inverse orphans given writers or deleted. |
| 3b | **The level fact** (ruling S8) | **2** estimators competing to answer one question, and a comparator that flagged their disagreement as a defect | **1** definition — matched acoustic output through the handover region — computed by **1** estimator (`solve_branch_trims`), with the passband estimate **kept and disclosed** as the starting estimate, never silently reconciled. Setting precision ≤0.5 dB; the 3.0 dB disclosure trigger survives as a separate knob. |
| 3c | **Front-end sharing** (MS-17) | two front ends, no enforced seam between them | **the wizard and the arm runner import the same engine verbs**; the engine has **zero mover-conditional branches (AST-verified)**; and the unified record is **byte-identical in shape regardless of mover**. A third mover is added with no engine edit. |
| 4 | **`crossover_v2_flow.py`** | 13,459 lines | **≈1,500** — `V2FlowSeams` (107) + `V2ConductorSnapshot` (87) + `attempt_history_from_state` (69) + `_CloudPosition` (54) + 47 read-only accessors (402) + a much smaller constructor + `hydrate`/`snapshot` (64) + journey delegation. A session record, its seam bundle, and its serialisation. |
| 5 | **`CrossoverV2Session`** | 6,753 lines · 156 methods · 102 attributes · a **776-line `__init__`** | **dissolved** into its four destinations: volume owner **1** · capture record **44** · phase machine **22** · a session-identity builder **35**. |
| 6 | **`web/correction_crossover_v2.py`** | 9,563 lines | the apply/rollback transaction (1,185) plus route wiring. *No explicit target exists in the evidence base — see §6.* |
| 7 | **Net lines at the END STATE** (ruling S4) | — | **NEGATIVE. Stated floor: −90,000.** Table below. Temporary adds during a wave do not count against this; scaffolding that would need deleting is not built at all. |
| 8 | **The class-A suite** | 149 files / 126,663 lines | **green**, running against the new engine. Reference: 410 passed / 24 skipped / 10.6 s across three flagships at `e064fa43d`. |
| 9 | **The baseline campaign reproduced** — *an instrument-validation act, not a measurement campaign* (S11) | `captures/postfix-baseline-2026-08` r1 + r2 | re-run on the new engine, **within the campaign's own measured noise floor: worst round-to-round change ≤ 0.37 dB**, 16/16 captures with the fader held, 0 glitched captures. The report's own bar: *"anything smaller than about 0.4 dB is noise, not a result."* **Like-for-like note:** the per-driver reproduction is applied-graph-independent (it rides the session measurement graph), but the **entry-baseline summed capture is not** — compare it against the same applied graph as the original, or disclose the delta (§6 R4). *[Amended 2026-08-29: this row MERGES with row 10 — the 0.37 dB reproduction bar is retired as an acceptance gate; its mechanics survive as the campaign's day-1 preflight — see [ADR-0192](adr/0192-the-campaign-is-the-validation.md).]* |
| 10 | **Then the real thing** — **only after every row above closes** (S11) | — | the owner's acceptance bar, quoted from that report: *"the full candidate campaign — many candidates, each measured, one winner, and the winner re-measured."* Entire trusted range, multi-candidate, best-of final, re-measured. **And the campaign OPENS with instrument bring-up in roster order — R-1 → R-5a, with R-5b/R-6 when the owner provides hardware** (the INSTRUMENT ROSTER below). *[Amended 2026-08-29: rows 9 and 10 MERGE — the campaign is now the validation, opening with the new baseline measured twice back to back to set its own noise floor, not a reproduction of row 9's retired one — see [ADR-0192](adr/0192-the-campaign-is-the-validation.md).]* |

**Row 7 has a measurement instrument now — stop hand-counting.** The audit program
is building **`scripts/right-size-report.sh`** with committed baselines: per-zone
comment-to-code, test-vs-product, and a dead-code scan. **This plan's net-lines
evidence consumes that report's census-zone output at every wave end**, instead of
a hand count. Two things that buys: the number is produced the same way twice, and
the baselines make a wave's delta a *measured* delta rather than an asserted one —
which is the same standard §5 already holds the acoustics to. Re-baseline against
the report at each wave close, and let it settle the function-grained re-triage
caveat under the net-lines table.

**Row 9 is the engine's proving ground.** The baseline campaign is the only
measurement in the tree that ran the whole loop twice under held conditions and
published its own noise floor. Reproducing it on the new engine, inside that
floor, is what turns "the refactor didn't break anything" from an assertion into
a measurement. Run it **before** row 10, not after.

### THE INSTRUMENT ROSTER — the campaign's opening phase (after acceptance closes, per S11)

Owner ruling, 2026-08-25: **"we need a comprehensive suite of tools to properly
diagnose and prescribe our speakers."** This is that suite, ranked by
`13-first-principles-gap-audit.md`'s cost-to-goal order — cost to the two things
JTS actually does with measurement, **VERIFY** and **LINEARIZE**, not distance
from any textbook.

**Nothing here runs during the refactor.** Ruling S11 reserves the hardware for
five enumerated validation acts and **the roster is not one of them** — see the
licence note at the end of this subsection. No wave owns these; they are built
after the engine lands and run once acceptance row 10 opens.

**Every instrument is a parameterization of the four verbs. Not one needs a new
one.** That is the point worth recording: the whole roster lands as `measure`
parameters and `analyze` metrics. It is **entry 3 on §1's absorption tally** — the
running list lives there, stated once. A vocabulary that absorbs a comprehensive
diagnostic suite without a fifth verb is a vocabulary that was cut at the right
joints.

**But an instrument is CODE; a preset is DATA — do not blur them.** Each roster row
is real work: R-3 is an `analyze` function that does not exist, R-4 is a `measure`
parameterization plus its consumer. What §1 calls data is the **preset that composes
already-built instruments over already-supported positions**. Building R-1…R-6 is
engineering; assembling `full-cloud` out of them afterwards is writing a parameter
bundle.

| # | Instrument | Verb shape | What it unlocks | Evidence |
|---|---|---|---|---|
| **R-1** | **Reverse-null** — shipped in three parts, executor deleted, **never run** | `measure(polarity=inverted)` + `analyze(null_depth)` | The guide's standard level **and** time diagnostic in one act — *"one measurement, two answers"* — **and** one of the two probes `docs/attribution-stage-plan.md:349` declares **"both required"** for the live M1 question. **Smallest step, biggest diagnostic.** *[Amended 2026-08-29: ruled BUILD, in flight as its own PR — see [ADR-0192](adr/0192-the-campaign-is-the-validation.md).]* | `13 §2.5`, `§H6`, `§"gaps 1 and 5 are jointly blocking"` |
| **R-2** | **Wider horizontal orbit + DI consumption** | `measure(positions=[…±30, ±45])`, then give the DI / per-angle model its **first production caller** in `analyze` | Measured **DI continuity at Fc** — the strongest listener-preference correlate the guide cites (Toole) — and the **−6 dB @ 30° ceiling, which is uncomputable from today's set**. Closes the doctrine's own violation: *"every mic movement gathers the maximum information it can support."* | `13 §2.2`, `§1h-angles`, `§H2` |
| **R-3** | **Near-field → far-field splice** — *capture SHIPS; the splice is the stub* | one `analyze` function over **two record kinds that already exist** | Lowers the **357 Hz** trusted floor; unblocks the explicit baffle-step model; and is the **named discriminator** for the campaign's one unexplained **~810–1055 Hz** feature. *[Amended 2026-08-29: PARKED — see [ADR-0192](adr/0192-the-campaign-is-the-validation.md) ruling 3: the room-comb ruling's remedy is now gating, not near-field.]* | `13 §2.3`, `§1h`, `§1e-note` |
| **R-4** | **Distortion as a design input** | a distortion-vs-**level** `measure` parameterization + the `analyze` consumer that turns it into a measured floor | Lets the tweeter floor be **confirmed or moved on evidence** instead of read off a datasheet — the guide's §1c method. *[Amended 2026-08-29: PARKED alongside R-3 — see [ADR-0192](adr/0192-the-campaign-is-the-validation.md).]* | `13 §2.4`, `§H5` |
| **R-5a** | **Vertical axis via a HUMAN mover** — **mic-only, no hardware gate** | `measure(position_axis=vertical, …)` + a preset + MS-17's `prompt` carrying the placement instruction | The crossover axis, reachable **today** with a person and a stand. Joins the buildable roster behind R-1…R-4. | owner ruling S12; derived from MS-17 |
| **R-5b** | **Vertical axis via the ARM** — **HARDWARE-GATED, owner decision** | the same parameters, driven by a positioner | Repeatability and unattended walks at vertical steps; master-plan **E1**'s 5° ladder. | `13 §2.1`, `§1f`, `§H1-b`, master-plan **E1** |
| **R-6** | **Impedance** — **HARDWARE-GATED, owner decision** | a sense-resistor jig feeding `measure`; two schema slots already wait | Genuinely **low cost for JTS** — we design no passive network, and the one place impedance enters arithmetic takes a declared nominal and refuses to invent a default. | `13 §2.7`, `§H4` |

**What each hardware gate is actually asking for.** R-5b needs **rig capability
for elevation** — our code names the axis (`crossover_v2_flow.POSITION_ROLE_XOVR`,
*"the axis the woofer/tweeter crossover lobes on"*) and then makes it unreachable
(`position_angle_deg` raises; `pose_at_angle` calls elevation *"the ratified
deferred axis"*; `CLOUD_VERIFY_POSE_PROMPTS` is *"vertical-free BY
CONSTRUCTION"*). R-6 needs **a sense-resistor jig**. Until the owner provides
either, **the corresponding blind spot stays stated and disclosed** — which is
S10's spirit applied to a measurement we cannot take: it never silently degrades a
verdict, it says what it could not see.

**The R-5 split is OWNER-RULING-DERIVED, and fragment `13` is silent on it — say so
rather than implying support.** `13` frames vertical as hardware-blocked throughout
(§2.1, §6 item 1 *"Blocked on elevation capture hardware"*, §3 item 4 *"no plan owns
shipping it"*), because it read the code's framing and the code is arm-centric. The
split follows instead from **MS-17**: the engine cannot know who moved the mic, so it
cannot require an ARM for an AXIS. A person and a stand satisfy the *"mic is at
position P"* precondition exactly as a positioner does, and `jasper-angle-capture`
already carries the human mover as a first-class concept (`MOVER_MAX_ANGLE_DEG` =
45 arm / **80 human**).

**But R-5a is mic-only, NOT zero-code — do not budget it as free.** Three sites
deliberately refuse a vertical pose today: `position_angle_deg` **raises**
(*"an external positioner cannot raise or lower the microphone"* — note it says
*positioner*, which is the seam the ruling reopens), `pose_at_angle` calls elevation
*"the ratified deferred axis"*, and `CLOUD_VERIFY_POSE_PROMPTS` is *"vertical-free BY
CONSTRUCTION"*. A fourth site, `REMOTE_VERTICAL_DISCLOSURE`, currently **tells the
household vertical is not covered** and must be updated when R-5a lands, or the
speaker will disclose a blind spot it no longer has. Undoing a deliberate constraint
is real work — it is just work that needs no purchase order.

**Three notes the roster must carry, or it imports a stale claim.**

- **R-2's assets are all built and none are connected.** The arm reaches **±45°**,
  a human **±80°**, `jasper-angle-capture` already accepts **arbitrary whole
  degrees**, `flat_spec_views.directivity_table` already computes measured
  per-angle directivity, and `forward_model.predict_sum` already predicts the
  complex sum **per angle** — with **zero production callers**. The shipped walk
  takes five angles and stops at ±22. In `13`'s words: *"We take off-axis captures
  and then drop them on the floor for design purposes."* R-2 is a **program
  change, not a build**.
- **R-4 must not import the guide's stale Adoptions claim.** That header claims a
  *"slope-aware distortion-informed tweeter floor"* was adopted; `13 §H5` found
  **zero hits for `slope_aware`**, and the slope relationship runs the **opposite**
  way — filter order is a second bound a steeper filter must **satisfy**, never a
  lever that buys a lower corner. Build R-4 as *distortion-informed*, and leave
  slope out of it. (See also the small-docs item in wave 7 that fixes the header.)
- **R-6 collides with the deletion waves, and the owner should settle it in one
  sentence.** `bass_extension/profile.py:impedance_import` and
  the relay's `build_bass_nearfield_spec` are fully validated, fully
  serialized, and have **no producer and no consumer anywhere** — `13 §2.7` names
  them as clean right-sizing candidates, while this roster calls them *"two schema
  slots already wait."* Both readings are correct and they point opposite ways.
  **Decide once:** delete them in wave 8 and re-add with the jig (the default the
  deletion rules and "every write needs a reader" imply), **or** keep them
  explicitly as declared-reserved with a note saying so. Silence resolves to
  deletion.

#### R-1's reading table, kept from the earlier draft

Invert one driver's polarity and re-measure. A **deep, symmetric null centred at
Fc** verifies **level and time simultaneously**.

| What you see | What it means |
|---|---|
| Deep symmetric null at Fc | Level and delay are both right |
| **Shallow** null | Level or slope mismatch |
| **Offset** null (not centred at Fc) | Delay error |
| **< 15–20 dB** null after delay optimisation | **Revisit levels before any EQ** |

Plus the **in-phase sanity check**: correctly matched and time-aligned drivers
sum to **+6 dB** at Fc.

*One number to carry into R-1 and the R-5 pair, corrected by `13 §H1-b`:* the stable
measured fact for this speaker is the **direct-arrival gap, −405.7 ± 3.3 µs
(n = 33)**, banked as `BASIS_US`. A **+314 µs** figure appears in the tree as
**one of three mutually inconsistent optimizer outputs at one physical
configuration**, cited there as evidence the automatic solver is unreliable —
never as a measured fact. Do not quote it as one. *(Swept: it never entered this
plan; recorded here so it cannot.)*

#### Prior specification — cited, not competed with

The roster does **not** mint a second planning authority. Its measurement content
is already specified in **`docs/tuning-master-plan.md`**, the program's planning
authority, and the roster **absorbs those unbuilt waves by reference**:

| Master-plan item | Roster home |
|---|---|
| **Wave 4.2 — Nearfield splice v1** (fully specified: 0.055×D placement, the Struck & Temme enclosure bound, port scaling by √(Sp/Sd), baffle step from f₃ ≈ 115/W) | **R-3** |
| **The 13-pose `baseline` program** (0°, ±10°, ±20°, ±30°, ±40° horizontal; 0°, ±10°, ±20° vertical; plus nearfield per woofer) | **R-2** (horizontal) + **R-5a / R-5b** (vertical — R-5a needs no hardware) |
| **The `verify` program's reverse-null**, and **E1** (lobe-tilt resolution) | **R-1**; **E1**'s 5° ladder is **R-5b** |
| **Wave 4.1 — Butterworth support** | **Not ranked.** `13 §2.6` rates it low cost and the guide itself makes LR4 the default. It stays with the master plan, unabsorbed and unhidden. |

**Wave 7e's doc consolidation carries this forward**: when the master plan folds
into the merged operational runbook and the engine design doc, these waves travel
as specification, not as a competing roadmap. That is the "one planning authority
per domain" rule being obeyed rather than restated.

#### The licence is unchanged

**S11's sanctioned validation acts stay exactly six** — the original five plus
the act 6 the owner ratified on 2026-08-26. This roster is
**post-acceptance** and adds nothing to that list. Each instrument must be
**added to the sanctioned list explicitly before it runs** — which, after
acceptance closes, is simply the campaign beginning. Nothing here may be read as
mid-refactor hardware licence. And **none of it touches the audit program's
zone**: the roster is measurement-program content, checked against §6 R5's
boundary table and clean of it.

### The net-lines table

| Row | Lines | Source |
|---|---:|---|
| Two re-export doors | −271 | `06 §Judgment 5` |
| Guard machinery 7,436 → ~600, **less 275 already banked** by the governance-reset branch | **−6,561** | `07 §Guard-test census`, re-baselined by the audit-program reconciliation |
| Two HANDOFF appendices | −8,043 | `07 §Consolidation map` |
| Linearization plan family, 5 files → 1 | −4,527 | `07 §Consolidation map` |
| Class C + D test deletion (floor) | −29,300 | `10 §Totals` |
| Census-zone prose | −30,000 | DEEP-AUDIT §4.5 |
| Census-zone test docstrings + fixture dedup — **residual, after S7's deletions take theirs** | −15,000 | S7 (revised down from DEEP-AUDIT §4.5's ~25,000) |
| *WITHDRAWN by ruling S2 — `commissioning_capture_producer.py` + its tests* | *(−2,089)* | *the lane is fixed, so the module gets a producer, not a grave* |
| **Deletion subtotal** | **−93,702** | 93,977 − 275 |
| Adds: engine test double (reference 1,948), engine design doc ≤800, runbook ≤600 (replacing 670 + 1,068), the #2202 producer, the little SQLite index, the ADRs | ~+2,000 | `10`, `07`, S2, S7 |
| **Stated floor** | **−90,000** | net −91,702; reserves **1,702** against the re-triage below |

**The floor moved, and it moved the wrong way — say why.** It was **−95,000**
before ruling S7 and is **−90,000** now. Nothing was lost; a number was corrected.
The old table counted ~25,000 lines of census-zone docstring trimming *and*
29,300 lines of whole-file test deletion, and I flagged that those two rows might
double-count. **S7 nets them out explicitly: the deletions take their docstrings
for free, so the docstring row is a ~15–20K RESIDUAL.** This plan books the
conservative end. So the old floor's ~9,000-line overlap reserve is retired, the
caveat it existed for is **closed**, and the honest number is smaller and firmer
than the number it replaces.

**Then the reconciliation moved it again, by 275, and the floor held.** The
governance-reset branch had already deleted 275 lines this plan was still
counting, so the subtotal is **−93,702** and the net is **−91,702**. The stated
floor stays **−90,000** — the reserve narrows from ~2,000 to **1,702**, which is
still real margin, and re-rounding to −89,000 would trade a true number for a
tidy one. Said out loud so the third movement of this figure is as traceable as
the first two.

**Two caveats still travel.** First, the withdrawn `commissioning_capture_producer`
row is shown rather than quietly removed: ruling S2 costs this plan a booked
deletion, and a number that moves without a reason reads as an error. Second —
and this is the live one — **the 29,300 figure is FILE-grained and S7 made
deletion FUNCTION-grained.** It will move in both directions: functions inside
class-E files become deletable, and functions inside class-C files get spared into
the class-B rewrite queue. `10`'s own framing (*"read it as a floor, not a
promise"*, against a 37-file / 81,699-line ceiling) was a file-grained bracket,
and **S7 retires that bracket rather than picking an end of it.** Re-count per
wave against the actual tree, using the §3 triage pass — not against either
end of a range that no longer describes the method.

---

## 6. Risks, and where the evidence ran out

**R1 — Class B is the least-verified class, and it is where the rewrite work
lives.** 119 files / 117,797 lines, largely read at `meta` and `skim` depth, and
**nothing in the census was run except class A**. `10` says it twice: *"if wave 2
buys one more pass, buy it there."* **Action:** buy that pass during wave 1,
before wave 2 commits to a schedule.

**R2 — Deferred imports defeat every top-level grep, and this package uses them
as a matter of course.** An AST walk finds **77 coupled test files / 123,778
lines**; a top-level grep sees **51 of the 77 and misses 52,474 lines**. `04`
recorded three of its own findings as wrong with one shape — *"reading a
refusal-shaped name as a refusal without following it to its outcome"* — and
`00 §7.1` is a fourth from a different fragment. **Any "X is dead / X refuses / X
has no caller" claim needs its outcome followed, not its name matched, and any
coupling claim must come from an AST walk.**

**R3 — The AEC fence is real even though the signal is safe.** The session graph
is **a new place where a format and a period get declared**, and a commissioned
`K` is valid only for the exact geometry it was measured against. If the session
graph's declared geometry differs from the applied graph's, **every commissioned
chip-AEC box parks with `CommissionRequired`.** Check once, before wave 6's
design freezes (MS-7). That park is the AEC side's clamp-class call and stays —
**ruling S10 does not reach across the fence to demote it** (MS-7's note).

**R4 — jts3 is a TEST BOX. Apply whatever you need to test.** *(Owner ruling,
2026-08-25, overruling the inherited warning this entry used to carry.)* Verbatim:
*"who cares if it wipes out a tournament winner? We don't care about the
tournament winner. In general, we should apply stuff to jts3 when we have
something we need to test."*

The box currently sits `blocked` / `active_baseline_topology_changed` because
entering `driver_style = cone_driver` rotated the topology fingerprint. **Clear it
however is convenient — including by applying the compiled candidate.** The
previous version of this entry told a fresh session never to do that, on the
grounds that it would wipe a tournament winner's blend correction, tweeter
linearization and level trim. **The owner has disclaimed that preciousness: the
winner is not precious, and its config is recoverable from the banked campaign
artifacts if it is ever wanted.** A warning that costs a session an hour to
protect something nobody values is a tax, not a safeguard.

**What survives the ruling, and it is the load-bearing half.** The sealed r1/r2
baseline remains the proving ground **regardless of what is applied to the box**,
and the reason is structural rather than procedural: **the per-driver
reproduction — the core of acceptance row 9 and of the 0.37 dB noise floor —
runs through the session MEASUREMENT graph, not the applied production graph.**
The per-driver phases (`PHASE_CHECK`, `PHASE_MEASURE`, `PHASE_LATERAL`) are
exactly the complement that pays the swap today and rides the session graph after
wave 6; the summed phases (`SUMMED_SWEEP_PHASES`, including
`PHASE_ENTRY_BASELINE`) are the ones that play into whatever graph is live
(`09 §1.2`). So applying a candidate cannot move the numbers row 9 is measured
against.

**One comparison IS applied-graph-dependent, and it gets a note, not a
prohibition:** the **entry-baseline summed capture**. Compare it like-for-like —
same applied graph as the original, or disclose the delta. That note now lives in
row 9 itself.

**Safety framing, per S10: this is a QUALITY unknown, never a safety one.** Every
compiled graph carries the per-driver protections **structurally** — MS-13's
`_assert_program_graph_proven` refuses to return a program graph whose tweeter
output lacks the high-pass and the soft-clip limiter together on exactly the
tweeter channels. An unmeasured candidate may sound worse. It cannot be unsafe by
this mechanism. **"Never apply it because it is unproven" was precisely the nanny
class S10 abolished** — refusing to WORK on an attestation gap — and this entry
was carrying an instance of it. Naming that is the point: the plan wrote S10 and
then kept a live example of what S10 forbids, three sections away.

**Wave 7j is unchanged, and its rationale is now stronger.** The staleness block
still dies. It was defending exactly the preciousness the owner has explicitly
disclaimed, so the demotion is no longer only a doctrine correction — the thing
the block was protecting turns out not to want protecting. Until 7j lands, the
state is merely inconvenient rather than dangerous, and clearing it needs no
ceremony.

**R5 — Coordination with the audit program: ACKNOWLEDGED, with terms.** The
double-lock this section used to demand has cleared. Both programs have now
reconciled **in writing** (audit-program reconciliation, 2026-08-25, via the
owner), and these are the terms:

| Boundary | Term |
|---|---|
| **The duck lease** | **CONFIRMED HELD**, in the audit agent's own words: *"I build no lease… the lease was designed for a world your waves 5e + 6d dissolve."* The mechanism that would have needed it is the mechanism this plan deletes. |
| **`test_lint_contracts.py`, `test_docs_handoff_freshness.py`** | **Ceded to us**, single-owner. The audit program does not touch either. |
| **`rust/jasper-fanin/**`** | Agreed protocol: their host-compliance PR carries the notice; **fan-in edits FREEZE after it** until the 5-case stereo tap re-runs on the box; **wave 6 does not execute before that tap.** |
| **The asyncio-marker sweep** | Lands repo-wide **before our wave 0**, or scopes our prefixes out. Either way wave 0 does not collide with it. |
| **`doc-map.toml` zone rows** | Their removal **rides our PRs**, with a notice line. |
| **The volume surface** *(new — see below)* | Ours for the duration of wave 5. |
| **`experiments/usb-turntable`** *(new — see below)* | Shared seam; promotion is theirs, re-verification is ours. |

**The volume surface — a widening this plan asked for and got.** Wave 5 edits
files **outside the census zone**: `volume_coordinator.py`, `camilla.py`'s
fader/duck paths, `mux.py`'s writer sites, `control/volume_ops.py`,
`web/sound_setup.py`, `cli/aec_tune.py`, plus the three volume test suites. Single-
owner area 3 is therefore widened from "the duck machinery" to **"the volume
surface", for the duration of wave 5**: those files are ours while it runs, the
audit program touches none of them without our acknowledgement, and **our wave-5
PR bodies carry a one-line notice whenever they land outside the zone.** The
widening expires when wave 5 closes.

**The arm driver is a shared seam.** `experiments/usb-turntable` is the
robotic-arm driver our `measure` rounds use at **every angle** — it is
load-bearing for the north star's arm front end, and it currently lives outside
`jasper/`. The audit program carries an owner gate to **promote it into `jasper/`
or accept the anomaly**. If promotion lands mid-campaign, **the path move carries
a notice and our arm tooling is re-verified before any measurement campaign
runs.** Added to §3's SHARED-SEAM LIST.

The audit's waves 0–1 remain pure green light and are worth landing **before**
this program starts; they shrink the tree it rebases over.

**R6 — The coupling trap has a second face: the fixture.** 24 of 26 importers of
`crossover_v2_fixtures.py` want a session harness, and the repo has already been
burned once by letting a test file become a de-facto fixture library. Defer wave
1's twin "until the engine settles" and 54,000 lines of test have nowhere to land.

**R7 — Prose deletion can delete the only record of a ruling.** 60% of the god
files is prose and several rulings live nowhere else. Wave 8's ~30K is
category-scoped by hand, never a sweep, and the ruling extraction is a
prerequisite, not a nicety.

**R8 — Ruling S2 turns a free deletion into design work, and the evidence base
never scoped it.** Every source read for this plan assumed the commissioning
lane might be abandoned, so `commissioning_capture_producer` was priced as a
2,089-line orphan removal. Fixing #2202 makes the lane live, and then the real
question is one nobody has asked: **the eligibility receipt has a production
reader** — `read_commissioning_room_authority`, which denies on every call
today, now naming which of the five denial reasons in `_common.py` it was
rather than one opaque code (ADR-0196). Wiring a producer
means deciding what that receipt should *say*, which is a design question about
commissioning eligibility, not a plumbing fix. **Scope it on the box during the
#2202 fix, before wave 4 books an estimate** — that hour is **sanctioned S11
validation act 4**, and it is scoping, not commissioning. Two independent mechanisms were
blocking this lane and only one of them is now settled.

**R9 — GROUND SHIFT: the governance reset. Read this before your first edit.**
Disclosed by the owner after the gates closed. **No plan-shape change** — every
ruling in §4 stands and every wave keeps its target. This is an **execution
surface** change, and it is the one thing in this document most likely to be
stale by the time you read it.

A separate owner-directed agent is landing, from branch
`claude/codebase-complexity-audit-plynn4`: a **~200-line AGENTS.md charter**
replacing the 3,534-line doctrine · **`docs/adr/` with ADR-0001** (the
operating-model reset) · a whittled adversarial-review command scoped to a
**closed safety tier** · **deleted prose-pinning tests** · and its own campaign
plan at **`docs/REFACTOR-2026-08.md`**. Plus a global-rules widening: **a
project's deletion mandates win.**

Five things the executing session must handle:

- **(a) RESOLVED — citations now name the charter's own sections.** This plan's
  review-gate and clamp-tier language cites the charter's **Non-negotiables** (the
  closed clamp list, *"nothing else is safety"*, and nanny demotion) and its
  **Review policy** (scale ceremony to risk). Substance preserved exactly; the
  old *"`AGENTS.md §Right-sizing directive`"* pointer is retired. Still **read the
  charter at session start** rather than trusting a paraphrase.
- **(b) RESOLVED — wave 7h re-baselined, with facts.** The governance-reset branch
  deleted **`test_agents_md_toc.py` (108)** and
  **`test_doc_staleness_sweep_20260604.py` (167)** — **275 lines already banked**.
  It did **NOT** touch `test_lint_contracts.py`, `test_docs_handoff_freshness.py`,
  or `test_crossover_v2_measurement_doc_pins.py`; all three stay ours. Wave 7h's
  row is **−6,561**, the subtotal **−93,702**, and the floor holds at −90,000 on a
  1,702-line reserve. The map tension resolved the same way it was framed —
  reconcile, don't alarm: the two contested files were **ceded to us in writing**
  (see R5).
- **(c) Our ADRs run ADR-0002 – ADR-0099; the audit program takes 0100+.**
  ADR-0001 is the operating-model reset.
  Wave 8's ADR mechanism and the four `00 §R5` rulings start at 0002.
- **(d) Cross-reference `docs/REFACTOR-2026-08.md` and this plan through the
  coordination map**, so neither claims the other's zone. Two campaign plans
  naming overlapping work with no supersession line is the exact defect
  AGENTS.md's "one planning authority per domain" rule exists to catch — and the
  five silently-competing tuning roadmaps of 2026-08-21 are the worked example.
- **(e) RESOLVED — wave 7i is DROPPED.** Its rule (*a doc may state a fact once;
  if a second doc needs it, the second doc links*) is already owned by the
  charter's **Docs default**: *do not restate here, in README, or in code what
  another file owns.* Adding it would be a second file restating what the charter
  owns — the very thing the rule forbids. The rule is kept by obeying it. The
  charter's ≤220-line budget is preserved untouched.

### Where the evidence base was not enough to plan — stated, not papered over

1. **"Capture paths → 1" has no number behind it.** The phrase does not appear in
   fragment `02`, and no current count of capture *paths* exists anywhere in the
   inventory. What `02` does count is **102 record types** and **4 same-fact
   duplications**; what `03` counts is **7 capture phases, 3 of which require the
   swap**. Acceptance rows 2 and 3 are written against those counted things
   instead. If the owner wants a literal "one capture path" criterion, it needs
   defining before it can be measured.
2. **No target line count exists for `web/correction_crossover_v2.py`.**
   `06 §Judgment 4` gives the flow file's irreducible core (~1,500 lines,
   enumerated part by part) and gives nothing equivalent for the web file.
   Acceptance row 6 names its residue (apply/rollback 1,185 + route wiring)
   without a number, on purpose.
3. **`10` carries two internal inconsistencies this plan does not resolve.** It
   states **23 distinct symbols** but enumerates 21 named plus one family (`PHASE_*`),
   and it says **9** files take a bare `import crossover_v2_flow` in one section
   and **8** in another. Wave 0's worklist should be built by re-deriving the set
   against the tree, not by copying either figure.
4. **`00 §7.6`'s "4 live docs, ~2,400 lines" does not reconcile** and this plan
   does not repeat it. Three authored docs with explicit targets total **1,550**;
   the fourth, `testing-tooling.md`, is 3,381 lines today and `07` explicitly
   keeps it *"because it is checked by use, not by a guard."* §5 carries the 1,550
   and states the index separately.
5. **Whether every integrity refusal still banks is unverified** for the capture
   screens and the seat-level ramp (verified only for the adoption path), and
   `08` found `SCREEN_SNR_FLOOR` does **not** bank at round granularity. That is
   proposed rule 3 of the integrity class, and it should be tested **before** the
   class is written into doctrine in wave 7a — the rule may need a stated
   carve-out rather than being asserted absolute.
6. **`09` executed nothing.** Every "this test would fail" claim in the contract
   section is derived by reading an assertion against the planned change; no
   mutated tree was run. The three failure shapes for the graph-emit guards are
   inferred from harness code, not observed. Expect surprises in wave 6c.
7. **Six of the audit's "roughly a dozen" swept-in seam tests remain
   unidentified.** The census regex was never disclosed; `09 §5`'s sibling set is
   a symbol sweep, explicitly *"my best reconstruction, not a closure."* The
   must-survive list is a floor on which seam tests break, not a bound.

---

*Draft written 2026-08-25 at `e064fa43d` by the tuning-stack conductor, from
fragments `00`–`11` in this directory, `docs/DEEP-AUDIT-2026-08-25.md`
(`origin/claude/codebase-complexity-audit-plynn4`), `docs/measurement-loop-doctrine.md`,
and `captures/postfix-baseline-2026-08/`. Every count cites its source. Nothing
was re-derived; where a number does not exist, §6 says so.*

---

## Appendix A — the invariant→pin table (PR 0d; updated as waves land)

Every §2 invariant against the test function that pins it, verified by grep at
`HEAD` rather than carried from `09` (which executed nothing). **No row came
back unnamed**, so no pin was owed. Status is `named` (exists today) or
`WRITTEN-NEW` (this PR). Every deletion wave checks against this table; a
deletion that would leave a row unpinned is blocked until the replacement pin
is written.

| # | Invariant | Pin | Status |
|---|---|---|---|
| MS-1 | Whole device contract, or none — every `ActiveEmitDevices` field derived and forwarded | `tests/test_ring_active_endpoint.py::test_every_emit_devices_field_reaches_the_emitter` | named |
| MS-2 | Both ends move together — under `shm_ring` a graph's capture and playback halves move on the same rung | `tests/test_ring_active_endpoint.py::test_the_capture_device_comparison_names_the_quiet_trap_not_every_graph` | named |
| MS-3 | One wire — one `resolve_ring_wire` format on both ends, plus the `RING_CAMILLA_*` geometry | `tests/test_transport_endpoint_preservation.py::test_boot_anchor_derives_the_ring_device_block` | named |
| MS-4 | Stimuli enter pre-DSP — a renderer-lane ring, never the post-crossover active ring | `tests/test_ring_active_endpoint.py::test_both_rings_are_forbidden_test_pcm_targets` | named |
| MS-5 | Every ring-naming emitter asks the width via `_assert_ring_playback_width` | `tests/test_ring_active_endpoint.py::test_the_width_refusal_actually_fires_through_an_emitter` | named |
| MS-6 | No full-range graph on a roleful box | `tests/test_ring_active_endpoint.py::test_the_flat_lane_is_refused_on_a_roleful_box_so_its_ring_kwargs_cannot_stomp` | named |
| MS-7 | A commissioned chip-AEC box whose final-edge geometry moved **applies its banked K and discloses** — it does not park (ADR-0101) | `tests/test_aec_init.py::test_a_commissioned_identity_that_moved_is_applied_and_disclosed` | re-pointed (wave 6b) |
| MS-8 | A tone must fit its fan-in test lease | `tests/test_commission_tone_single_owner.py::test_commissioning_tone_fits_inside_mux_gate_lease` | named |
| MS-9 | A secondary DSP instance fails closed to silence, never to a reboot | `tests/test_camilla_crossover_unit.py::test_unit_never_reboots_the_box` | named |
| MS-10 | A blocked graph-repair leaves the statefile byte-for-byte untouched | `tests/test_camilla_crossover_guard_script.py::test_runtime_contract_blocked_leaves_statefile_untouched` | named |
| MS-11 | The fan-in gate is owner-scoped — select and release name the same owner | `tests/test_commission_tone_single_owner.py::test_commissioning_uses_its_owner_scoped_mux_gate` | named |
| MS-12 | Commission-tone orchestration has exactly one owner module | `tests/test_commission_tone_single_owner.py::test_sound_setup_imports_commission_tone_constants_from_owner` | named |
| MS-13 | The program graph is role-routed, crossover-free and tweeter-guarded, or the emitter refuses | `tests/test_active_speaker_program_config.py::test_program_config_passes_all_graph_safety_proofs` | named |
| MS-14 | Every stimulus plays at the declared level, proven, or not at all | `tests/test_crossover_v2_measurement_volume_drift.py::test_a_drifted_fader_refuses_the_capture_before_any_audio` | named |
| MS-15 | The lane wire is a boot-time fact with zero writers | `tests/test_ring_wire_format_contract.py::test_the_wire_key_has_no_writer_so_the_rollback_lever_survives` | named |
| MS-16 | A stimulus WAV wider than the lane is silently downmixed — isolation is bounded at 2 ch | `tests/test_renderer_ring_lanes.py::test_the_confd_ring_slave_is_plug_wrapped_at_the_lane_wire` | named |
| MS-17 | Mover-agnosticism — the engine holds zero arm-specific and zero wizard-specific code | `tests/test_measurement_mover_agnostic.py::test_the_engine_imports_no_mover_and_no_front_end` | WRITTEN-NEW |

**Two rows carry an EXTENSION owed, not a missing pin.** Both are pinned for
today's *per-stimulus* emit, and neither extension can be written until the
session graph exists. **MS-1** is PC-3 verbatim: the named pin and its sibling
`test_the_crossover_v2_program_graph_follows_the_arm_in_both_directions` both
bind the call site this plan deletes, and the session-graph emit site joins
both in the same PR — the guard gets stronger there rather than dying with the
site it happened to watch. **MS-13**'s §2 clause "a session-scoped graph must
still pass it, once, before the first stimulus" gets its assertion in that same
wave.

**MS-17's pin is bounded to the import graph.** The arm driver is reached as a
subprocess tool path, not an import, so a hard-coded
`experiments/usb-turntable` path literal inside the engine is a review finding
the AST walk cannot see. Stated in the test rather than plugged.

**MS-7's row was corrected in wave 6b, and the invariant it stated was wrong.**
The pin this table named — `test_production_chip_profile_parks_when_final_edge_format_changes` —
does not exist at `HEAD`; the nearest surviving name,
`test_production_chip_profile_parks_when_nothing_is_banked_or_shipped`
(`tests/test_aec_init.py:224`), parks when **nothing is banked**, not when
geometry moves. The real behaviour is the opposite of the row's claim, pinned by
`test_a_commissioned_identity_that_moved_is_applied_and_disclosed`
(`:302`), which is parametrized over `("output_id", "different_dac")` — a
hardware-class field — and asserts `main() == 0` with the moved field named
in the disclosure file. Its in-test words: *"hardware-class divergence — the
loud kind, still not a park."* A sibling row in the same table now pins
MS-7's original subject directly: since ADR-0190, `output_format` is
recorded-only and moving it alone produces no divergence at all — nothing is
disclosed, not even the loud-but-not-parked kind.

**So wave 6's R3 tripwire is answered NO**, and neither link in its chain
exists. (1) `jasper/cli/aec_init.py:814-859` reads the final-edge geometry from
outputd's STATUS socket and env only — the module contains zero CamillaDSP
references, and outputd latches its DAC `hw_params` in a `OnceLock` at its own
ALSA open (`rust/jasper-outputd/src/state.rs:567-568`) — so a transient
`set_active_config_raw` graph is structurally invisible to it. (2) Even if
geometry did move, `aec_init.py:1065-1081` applies the banked K and returns 0
(`disclosed_stale`); `CommissionRequired` on a geometry field is reachable only
for a box with **no** commissioned artifact. Making the measurement graph
session-scoped therefore cannot park a chip-AEC box.
