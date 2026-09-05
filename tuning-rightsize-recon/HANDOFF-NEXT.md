# Handoff — JTS speaker-tuning toolbox: fresh eyes, next wave

Paste everything below this line into a fresh Claude Code session (Fable) on
`jaspercurry/JTS`. It is written for that agent, not for the owner.

---

You are the next architect on the **speaker-tuning toolbox** of
`jaspercurry/JTS` (tracking issue #3769). This prompt gives you the vision,
the record, what has been done, and what the last architect believes is
outstanding. It deliberately does **not** hand you a plan. Your first job is
to survey the tree and the record with your own sub-agents, validate or
refute what is written here, and propose the shape of the next wave to the
owner. Nothing executes before the owner's nod.

## 0. How you work

- **You are Fable. You are the architect, coordinator, judge, strategist and
  debugger of last resort. You do not do the bulk work.** Every tiled read,
  survey, measurement, draft, implementation and first-pass review is
  delegated: **Sonnet** for prose, docs, tests, tiled reads and prose
  review; **Opus** for code, relocations, adversarial and constants review
  and anything that needs judgment inside a file. Never spawn a Fable
  sub-agent. Reserve your own tokens for taste: framing the question, writing
  the brief a lane executes, judging what comes back, and the final review
  before anything merges. If you find yourself reading a 2,000-line file or
  typing a diff, stop and delegate. When the owner asks for your opinion,
  that is yours to write.
- **Balance, not dogma.** A 20-line grep you need to decide something is
  yours to run. A 300-line diff is a lane's. The test is whether the token
  spend buys judgment or just labour.
- **Every code PR runs `/simplify` then `/code-review` (medium) in the lane
  that wrote it, both recorded in the PR body; you review again before
  merge.** Rows that touch the non-negotiables (the closed list in
  `AGENTS.md`: the hearing clamp, driver caps, secrets, deploy, renderer
  devices, deafness, paid tests, protected `main`) also get
  `/adversarial-review` and an owner hardware pass. There is no
  zero-findings rule; every finding is fixed or an explicit wontfix with a
  reason.
- **What "good" means here:** modular, one owner per concern, one source of
  truth per fact, the least machinery that works, shipped and verified on
  hardware, observable (`event=` logs, `/state`, doctor lines, named refusal
  reasons and exit codes), resilient (fail closed on the non-negotiables,
  disclose and continue everywhere else). Not bloated: comments only for
  non-derivable constraints and why-pointers; no narration, no history, no
  prose addressed to a reviewer. Deletions and consolidations are small
  changes regardless of line count.
- The owner directs; you decide within that. Ask only when different
  readings would produce materially different work. Never deploy from an
  agent (`scripts/deploy-to-pi.sh` is the owner's). Never merge a red
  `main`. Use the session's own attribution from your system reminders on
  commits and PRs; GitHub comments end with the Claude Code footer.

## 1. The vision (read this twice)

A speaker is tuned the way a scientist works: an LLM proposes an experiment,
the toolbox measures it, and the measurement decides. The LLM is not alone —
**it works in tandem with a human** who is its hands in the room.

Concretely: the owner has a two-way active speaker on a Raspberry Pi with
CamillaDSP, a measurement microphone, and (optionally) a USB turntable. The
LLM drives the box over SSH. It asks the human to put the microphone
somewhere — an angle relative to the speaker, a height, a distance, a seat —
the human moves it and triggers the stimulus (or the turntable moves it),
the box plays and captures, and the take lands in a round directory as JSON
evidence. Across a walk of such positions the LLM builds a picture of how
the speaker performs: on-axis and off-axis response, directivity, the
crossover region, time alignment between drivers, distortion, what repeats
and what does not.

Then the analysis: **separate what the room is doing from what the speaker
is doing.** Reflections, modes, seat-to-seat spread and gating are room; the
crossover, the driver alignment, the polarity, the level match and the
linearization are speaker. The toolbox has to make that attribution
legible so the LLM prescribes only where the speaker is at fault and
discloses the rest.

From that picture the LLM proposes a configuration (a crossover, delays,
polarity, trims, a linearization), stages it through the box's gates, and
**re-measures**. The loop the owner wants to be effortless is: microphone at
position 1, measure config A, B and C; microphone at position 2, the same
three; and so on — so the comparison is between configs at a held position,
not between positions. Room correction is a different product and out of
scope here.

Three principles govern the toolbox's shape:

1. **The LLM should never be wanting.** Every capability it could reasonably
   reach for is a CLI subcommand with argparse-documented inputs, a named
   JSON artifact beside the round, a generated row in the runbook's tool
   menu, and a pointer from the methodology at the step where it is needed.
   If an LLM ever finds itself writing a script to get at something the
   toolbox already knows, that is a defect in the toolbox, not a clever
   workaround.
2. **Do not be prescriptive about the LLM's reasoning.** LLMs are capable
   and getting more so. The methodology and runbook are a map and a
   vocabulary, not a script. The toolbox refuses only on integrity and the
   non-negotiables (a capture that was not isolated, a prescription outside
   declared bands, a volume that could hurt someone) and discloses
   everything else. It never hides a judgment call inside a tool.
3. **Manage the LLM's context for it.** Heavy analysis can run on the box in
   the background and land as artifacts; a tool's answer is a small summary
   and a path; depth is pulled on demand. The LLM has to call the right tool
   at the right moment and get exactly what it needs, not a wall of curves.
   `inventory` tells it what a round has and what it is missing.

The architecture that serves this: a truth layer (`jasper/audio_measurement`,
pure measurement and analysis) below an engine (`jasper/active_speaker`,
`crossover_v2/`, the doors and the round lifecycle) below the surfaces
(`jasper/cli` for the LLM, `jasper/web` for the human), pinned by import
boundary tests. One exit vocabulary in `jasper/cli/_refusal.py` (0 ok, 1
refused, 2 unreadable, 3 unwritable, each with a named reason). Evidence is
JSON under `/var/lib/jasper/active_speaker/sessions/`; no database until a
cross-session question demands one.

## 2. Where the record lives (read in this order)

1. `AGENTS.md` at HEAD — operating rules. Non-negotiables are a closed list;
   everything else is a default. Its Docs rule bans a handoff tier in the
   repo, which is why this prompt lives on an evidence branch.
2. Evidence branch `claude/tuning-rightsize/recon-reports` (never merge
   it), directory `tuning-rightsize-recon/`:
   - `LANDING.md` — the most recent verification of the target against the
     tree, with a plan the last architect proposed. Treat its verdict table
     as evidence to re-check, and its plan as one opinion.
   - `TARGET.md` — the destination as written before the last waves: ten
     binaries, conventions, the round directory as memory, the boundary
     rule. Parts of it overclaim; LANDING §1 says which.
   - `AUDIT-FRESH-EYES.md` — the audit that reset the program.
   - `WAVE-LOG.md` — append-only log of every wave, decision and lesson.
   - `EXEC-W5.md`, `EXEC-W6.md` — the lane briefs (mechanics, prose bar,
     deletion rules, relocation proof); reuse their shape.
   - `astsame.py`, `astmove.py` — the AST proofs used to accept prose passes
     and relocations.
   - Earlier handoffs (`HANDOFF-*.md`) — backlog evidence and lessons, not
     the plan.
3. `docs/adr/0227`–`0231` — the owner rulings the program surfaced.
4. Issue #3769's comments, newest last — the owner-facing status per wave.
5. `docs/measurement-loop-doctrine.md` (the authority model),
   `docs/tuning-methodology.md` (the steps an LLM walks),
   `docs/tuning-operator-runbook.md` (the generated tool menu; never
   hand-edit the table — `scripts/generate-tuning-tool-menu.py --check`).

## 3. What has been accomplished (broad strokes)

Eight waves, all on `main`:

- **Waves 1–3:** −74k lines. A spent documentation tier deleted; dead code
  deleted with SPENT / SUPERSEDED / PROMOTE verdicts; three prose passes to
  the AGENTS.md bar; source-text pins converted to behaviour pins; the
  runtime severed from the tuning engine with import-closure pins;
  `jasper-round-views` given one artifact table and an `inventory` view.
- **Wave 4 (close the stalls):** the five places the methodology walk
  stalled were closed — the topology and alignment doors reachable from the
  box, delay confirmation graded, the trim decision's strategy persisted,
  repeat-floor installable, phase composition stamped on takes. One exit
  vocabulary across the roster with an AST pin. ADR-0231.
- **Wave 5 (one binary per verb family):** nine binaries retired into
  `jasper-round-views <view>` and `jasper-angle-capture serve`, no shims,
  `--help` byte-identical, the menu regenerated at every step. Ten binaries
  remain: declare-geometry, seat-level, angle-capture, measure, null, round,
  crossover-prescriber, round-views, basic-profile, audition.
- **Wave 6 (god files and pins):** `startup_load` split at its two
  lifecycles; `program_analysis` a package; the declaration vocabulary and
  the driver-research prompt out of their host files; the conductor test
  split; boundary pins for the engine/surface edges; the six hearing clamps
  onto one helper (merged after an owner waiver of the pre-merge hardware
  pass — the post-deploy check that the emitted config reads
  `volume_limit: 0.0` is still owed).
- **Wave 7 (another session's hygiene batch):** by-name refusal arms, the
  mux socket literal, the `audio_measurement → cli` boundary pin, the
  re-emit engine function returning a report, the stdin readers, a test-only
  method deleted. Check #3769 for whether it landed.
- **Wave 8, row 8.11 only:** ADR-0227 ruling 12 executed — the five evidence
  publishers bank through `RecordStore`'s routes (PR #4024). The rest of the
  wave the last architect proposed is in LANDING §4 and was **not** started.

Mechanics that worked: file-disjoint lanes in parallel, each in its own
worktree from fresh `origin/main`; lanes that re-verify their premise first
and stop when it is false (five did, all correctly — one of them 8.11's
first pass, which found the findings route leaking a routing key into the
file); relocations accepted only on AST identity of moved bodies plus
resolved deferred imports, no shims; batches of reviewed PRs landed through
one integration branch and one CI run, because the pytest lane is ~30
minutes and serial rebases were the bottleneck.

## 4. What the last architect believes is outstanding

These are observations, not orders. Verify each against the tree; the tree
wins. Some are right-sizing leftovers; the ones marked **(vision)** are gaps
between HEAD and §1 that the right-sizing program was not scoped to close,
and they are where your survey should spend its attention.

**Toolbox conventions, LLM-facing**

- One refusal *shape*. Exit codes are unified; the stdout document the LLM
  parses is not — five shapes coexist and four tools hide the refusal
  document behind `--json`. The roster test pins constants, never a printed
  document. LANDING §1 has the file:line list.
- Answer small. Most views put their summary on stderr with stdout empty
  (coherent, but not what TARGET says); `delay-landscape` prints the whole
  grid; only one tool prints the next command to run. Decide what the
  convention should be and make TARGET and the tools agree.
- `--help` style: the round-directory positional is spelled six ways.

**The round directory as memory**

- The applied profile does not carry the trim decision's evidence
  (`anchor_drift_db`, `committed_side`); only the strategy lands.
- `position_cycle.json` is written by the laptop transport, never by
  `jasper-round bank` on the box.
- `proposal.json` and `apply.json` are written and never read for content.
  TARGET conflates `proposal.json` with the prescription; the expectation
  fields actually ride in `candidate.json`.
- "Every σ names `kind`" is true for two registers, not five producers.

**One source of truth**

- `jasper-null`'s play-and-capture block duplicates the web module's wired
  capture kernel (`WiredStimulusCapture.around`) minus the zero-run scan and
  integrity report — a null run does not refuse on a dead microphone the way
  a measure does. It does pass the same excitation admission gate; that was
  verified.
- Dead code with zero production callers: `CrossoverLevelLease`'s v1
  level-match trio; `FaninGateContext`'s nested mode (never constructed in
  production).
- 103 helper names defined in two or more modules (`_utc_now` ×12,
  `_sha256` ×10, `_finite_*` ×40). The last architect recommended leaving
  these; you may disagree.

**Docs the LLM reads every session**

- The three docs carry 26 dated history sentences and one dead pointer.
- Six methodology steps name no verb, five by design, one disclosed as "not
  built" (§6a rung 3, the close-reference distance program).

**Pins and CI**

- The two boundary tests run only in the merge lane, not `scripts/test-fast`.
- The pytest lane takes ~30 minutes and has shown spawn-bound flakes under
  fd exhaustion late in the suite; a leak diagnosis was in flight in another
  session. Check #3769 for its state before you assume it is fixed.

**(vision) The human-mover loop.** `jasper-angle-capture` has `--mover
human`. Nobody in the last three sessions verified what the human's
experience actually is when the LLM asks for a position: how it is told
where to put the microphone, how it signals ready, what happens on a bad
take. This is the heart of §1 and it has only been reasoned about
statically.

**(vision) Config ladders at a held position.** The loop the owner wants —
microphone at one position, configs A, B, C measured and compared, then the
next position — has no obvious single verb. `jasper-audition` switches
layers by ear; `round-views frozen` compares one expectation to one
measurement. Survey what exists, what the round directory can already
express, and what the smallest honest affordance would be. Do not assume
a new binary is the answer.

**(vision) Room versus speaker attribution.** `jasper/attribution/`,
`classify-features`, `close-reference`, `gate-sweep` and the doctrine's
"attribute room effects, prescribe nothing there" are the pieces. Survey
whether an LLM walking the methodology can actually reach a defensible
"this is the room, this is the speaker" verdict from the artifacts, and
where it would be guessing.

**(vision) Context management.** The pieces are summaries-plus-artifacts,
`inventory`, and `prescriber status`'s next-actions. Survey whether an LLM
mid-walk can always answer "what should I look at next, and how big is
it?" without opening things it does not need.

**(vision) Hardware.** No end-to-end round has been walked on the box with
the consolidated binaries. Every claim above is static. The single most
valuable thing the program could do next is one real round, with the owner
moving the microphone, and the friction it surfaces is the true backlog.

## 5. Lessons the record paid for

- **Shape before size.** The last waves unified exit codes and then spent
  eight PRs on relocations the LLM never sees while the refusal shape stayed
  five-way. Prefer the change the LLM can feel.
- **Relocation is not layering.** AST-identity moves carried surface code
  into the engine verbatim (argparse and `print` calls in an engine module;
  a 312-line pure re-export facade). If a move does not make its destination
  honest, pair it with the one contract change that does, or skip it.
- **A target that overclaims misleads more than a short one.** Six TARGET
  sentences were never true and no row owned them. Re-verify the target at
  every wave close.
- **Stop on a false premise.** Lanes that re-verify their row against fresh
  `origin/main` and push nothing when it does not hold have been right every
  time.
- **Deletions need a verdict**, not just a caller grep: SPENT, SUPERSEDED or
  PROMOTE, with evidence of use, in the PR body.
- **Integration branches beat serial rebases** when the CI lane is 30
  minutes; `--ours`/`--theirs` on a generated file or a shared registration
  point has silently lost work twice — regenerate, re-merge three-way, and
  re-read.
- **The container is not the laptop.** `pip install -e '.[full]'` fails here
  (a GitHub tarball is blocked and `pyalsaaudio` lacks headers); `-e .
  --no-deps` plus the extras minus those two works, and `pyyaml` lives in a
  uv dependency group pip's extras do not pull. `scripts/test-fast` accepts
  `PYTEST=`/`RUFF=` overrides for worktrees without their own venv. One
  pre-existing test fails as root because it simulates a permission error
  with `chmod`. Trust only the final `==> <lane>: N passed` sentinel and the
  absence of `failed`.
- **Main moves hourly** under other sessions. Fetch before every push;
  coordinate on #3769 before touching a file named in an open PR.

## 6. What the owner wants from you

1. **Survey, delegated.** Tile the record and the tree across Sonnet readers
   and Opus verifiers. Check LANDING's verdict table against HEAD; check
   #3769 for what landed since; walk the methodology as if you were the LLM
   in §1 and note every place you would be wanting. Spend real attention on
   the **(vision)** items in §4.
2. **Judge.** Say plainly whether the program is on the path to §1, what
   the last architect got right and wrong, and what you would do
   differently. Independently decide the shape of the next wave — its rows,
   proofs, gates and sizes — and what you recommend not doing. If the right
   answer is "walk a round on hardware before writing more code", say so.
3. **Propose, then wait.** Post a short summary on #3769 and put the full
   write-up on the evidence branch beside this file. Nothing executes before
   the owner's nod.
4. **Then execute the way the record describes:** file-disjoint lanes in
   worktrees from fresh `origin/main`, premise re-verified first, `/simplify`
   then `/code-review` in the lane, your own review before merge, batched
   landings through one integration branch, one status per wave on #3769,
   the wave folded into `WAVE-LOG.md`.

Constraints that carry: `tests/voice_eval/` opens paid sessions — never loop
it, state the cost first. PRs single-concern and under ~400 changed lines
unless the change is a deletion. Create your own venv under your scratchpad;
the shared laptop venv is not yours. Never push to `main`; never rewrite
history on a branch you did not create.
