# The acceptance brief: what row 9 and row 10 actually demand

> **THIS DOCUMENT DIES WHEN ACCEPTANCE PASSES.** It is a readiness brief, not a
> planning authority. The bars belong to
> [`REFACTOR-TUNING-2026-08.md`](REFACTOR-TUNING-2026-08.md) §5; this file only
> says what state we are in against them, what a bench operator would actually
> type, and what the paper cannot answer. When row 9 closes and row 10 opens,
> delete it — the roster and the campaign live in §5, and the procedure will
> have been run.

> **[Amended 2026-08-29]** Rows 9 and 10 merged — this brief's row-9/row-10
> runbook is superseded by
> [ADR-0192](adr/0192-the-campaign-is-the-validation.md); read that first.

> **Chunk 3 of the tuning refactor.** Chunk 1 — waves 0–8 of
> [`REFACTOR-TUNING-2026-08.md`](REFACTOR-TUNING-2026-08.md) §3 — built the
> engine beside the god files. Chunk 2 —
> [`REFACTOR-CUTOVER-2026-08.md`](REFACTOR-CUTOVER-2026-08.md) — plans the
> cutover. This chunk plans the **proof**.
>
> **Nothing here is a hardware licence.** §3 drafted a sixth sanctioned act for
> the owner to sign or refuse; **he adopted it on 2026-08-26**, so S11's list is
> now six. The act itself is still blocked on a build that does not exist, and
> nothing else in this brief opens the box.

**STATUS — scouted at `c253c3cf1`, paper only. No hardware was touched.**

| § | Section | Status |
|---|---|---|
| 0 | Premise re-derivation ledger | VERIFIED-COMPLETE |
| 1 | Row 9 — the baseline re-park | VERIFIED-COMPLETE, three gaps flagged |
| 2 | Row 10 — the instrument roster's status | VERIFIED-COMPLETE |
| 3 | The S11 amendment for the producer build | **ADOPTED — owner-ratified 2026-08-26; now S11 act 6** |
| 4 | The chunk-3 order | VERIFIED-COMPLETE |

**The three things to read if you read nothing else.** (1) Row 9's pass/fail
instrument is a bespoke script chain inside a **gitignored** directory, not a
shipped tool — the tool the criterion names errors on this round shape by design
(§1.6). (2) 7j demoted the topology block, but **`driver_style` is not
metadata** and a second gate one step downstream is untouched, so "7j landed"
does not mean "the box opens" (§1.4). (3) **Two of S11's six sanctioned acts are
unrun** — 1 and 5 — and they batch into one bench evening in dependency order
(§4); **act 2 (no-pop) has a banked bounded PASS** on jts3 and joins that evening
only if the owner reopens its bound. (Act 6, adopted 2026-08-26, is a third unrun
act, but it waits on a build rather than on an evening — §3.3.)

---

## 0. Premise re-derivation ledger

Everything this brief was handed was re-derived at `c253c3cf1`. What moved is
recorded rather than silently corrected.

| Premise as handed | At HEAD | Disposition |
|---|---|---|
| The banked r1/r2 baselines are in the repo | **`captures/` is gitignored** (`.gitignore:38-40`, no trailing slash); `git ls-files captures/` returns zero rows. The campaign exists **only** in the main checkout at `/Users/jaspercurry/Code/JTS/captures/postfix-baseline-2026-08/`, mode `0700`/`0600` | **corrected, and it is load-bearing.** An agent worktree cannot see the proving ground at all. §1 gap 1. |
| "Two schema slots already wait" for R-6 (`:1356`) | **one.** The relay's `build_bass_nearfield_spec` was **deleted** by #3081 (`056cc8cfc`, 2026-08-26) with its `SHIPPED_KINDS`/`BUILDERS` entries and re-exports. `bass_extension/profile.py:211`'s `impedance_import` survives under **ADR-0018**'s park | **STALE — the roster line overcounts.** §2 R-6. |
| R-6 is an open owner decision ("silence resolves to deletion", `:1404-1413`) | **settled twice, in opposite-looking ways that are actually one answer.** #3081 deleted the relay half by owner ruling; ADR-0018 parked the `bass_extension/` half by owner ruling | **CLOSED.** §2 R-6. |
| #1738 is a live binary owner call — *"wire it up, or delete it"* (`:1268-1272`) | **ADR-0018: the owner chose a third option — PARKED.** *"I want to leave it parked."* | **CLOSED.** The plan's §4 tail is stale on this. |
| R-1 cites `docs/attribution-stage-plan.md:349` (`:1350`) | that file is at **`docs/historical/attribution-stage-plan.md`** since #2979 (`b25216fff`). The *content* holds — the M1 row still reads *"Reverse-null (P1) **and** design-axis/vertical-offset (P5) — both **required**"* | **path stale, claim intact.** §2 R-1. |
| Wave 7k (the guide's stale Adoptions header) is owed | **DONE.** `crossover-design-guide-deep-research-2026-08-19.md:1-45` now states the split explicitly and names the live slope-blind gates | **closed.** |
| The report's bar reads *"anything smaller than about 0.4 dB is noise, not a result"* (`:1302`) | source is *"any future **'improvement'** smaller than about 0.4 dB is noise, not a result"* (`report/derived/page.py:246-250`) | **paraphrase, not quote.** The plan renders it inside quote marks. Substance identical; the word `improvement` scopes it to deltas, which matters when someone points it at an absolute. |
| Row 7j: *"entering `driver_style` — **metadata** — rotates `topology_config_fingerprint`"* (`:1063`) | **`driver_style` is not metadata.** 7j's own rider `62c5402f3` records review R1 refuting it: it selects the tweeter's `min_highpass_hz` and sits in the **role-independent** driver-safety target match. HEAD's comment says so at `setup_status.py:1050-1054` | **STALE, and consequential.** The topology block died; a second gate did not. §1.4, gap 3. |
| S11 act 5 (7j's demotion verification) is available to lean on | **never run.** PR #3006: *"The on-box verification (S11 sanctioned act 5) is **deferred**"* — jts3 was running a non-ancestor build | **OPEN.** §4. |
| Wave 6d landed with the no-pop check as its licensing evidence | 6d **merged** at `f6a6c56f3` (#3111). Its body still carries the pre-merge staging text — *"**Not merged.** … the no-pop check on jts3 is the evidence that licenses it"* — but **the check ran and was banked before that merge.** The sanctioned-act record is a **comment on #3111** (2026-08-26T16:25:58Z), which is why a `captures/` search could not find it | **RECONCILED, not open: a bounded PASS is banked.** The bound is the owner's to close. §4. |

**One structural finding the brief was not handed.** Row 9's pass/fail number
is not produced by any shipped tool. The named tool — `jasper-round-views
repeat` — **cannot ingest this round shape**, and that was ruled a scope defect
rather than fixed. Detail and consequence in §1 gap 2.

---

## 1. Row 9 — the baseline re-park

### 1.1 What the row demands

Row 9 (`REFACTOR-TUNING-2026-08.md:1302`), verbatim on the bar:

> re-run on the new engine, **within the campaign's own measured noise floor:
> worst round-to-round change ≤ 0.37 dB**, 16/16 captures with the fader held,
> 0 glitched captures.

Three numbers, three different facts, and they are routinely blurred:

| Number | What it actually counts | Source |
|---|---:|---|
| **0.37 dB** | worst disagreement between r1 and r2 across **ten driver-and-angle pairs**. Typical 0.03–0.13 dB. Worst cell: **woofer at −22°, 0.369 dB, at 180 Hz** — the bottom of the woofer's fit band | `report/derived/page.py:246-250`, `:381-383` |
| **16/16** | **8 routed captures per round × 2 rounds.** Per round: `capture:check` 1 · `capture:measure` 1 · `capture:lateral` 5 · `capture:entry_baseline` 1. Every line reads `expected_db=-8.000000 observed_db=-8.000000 delta_db=0.000000 tolerance_db=0.050000` | `run-log.md:1396-1409` |
| **0 glitched** | zero `repairing` / `repaired` / `refused` events in either round's journal window | same |

It is *"an instrument-validation act, not a measurement campaign"* (S11), and
§5's preamble scopes it as **a development gate on merging a wave, never a
runtime gate on the speaker** (`:1285-1288`). It is also **not a whole-engine
proof** — *"the proving ground exercises none of these"*: failure-branch pins,
refusals, races, cancellation, retry, stall recovery (`:542`, S7 at `:1244`).

### 1.2 What the row does NOT demand — the like-for-like carve

The **per-driver reproduction rides the session measurement graph, not the
applied production graph** (`:1555-1565`) — `PHASE_CHECK`, `PHASE_MEASURE` and
`PHASE_LATERAL` are the phases that pay the swap today and ride the session
graph after wave 6. **So applying a candidate to jts3 cannot move the numbers
row 9 is measured against.** One comparison IS applied-graph-dependent and gets
a note, not a prohibition: the **entry-baseline summed capture** — compare it
against the same applied graph as the original, **or disclose the delta**
(`:1567-1570`).

### 1.3 Where the baselines live, and what a round is

**`/Users/jaspercurry/Code/JTS/captures/postfix-baseline-2026-08/`** — main
checkout only. There is **no README or doctrine file in `captures/`**; the
convention is carried by the runner's own flags (`--campaign` = campaign
directory, `--label` = the round's name inside it, both required to measure,
`scripts/run-crossover-round.py:1069-1075`, `:1210`) and documented at
[`docs/testing-tooling.md:2564-2596`](testing-tooling.md). Naming across the
183 sibling entries is `<topic>-<YYYY-MM>` or `<topic>-<YYYYMMDD[THHMMSSZ]>`.

| Path | What |
|---|---|
| `baseline-r1/`, `baseline-r2/` | the two banked rounds |
| `report/graphs.html` | **the report.** The 0.37 dB number lives here and only here |
| `report/derived/{replay,reduce,build_page,page}.py` | the analysis chain that produced it — campaign-local, untracked |
| `baseline-r{1,2}.runner.log` / `.trail.jsonl` | the runner's own record. `baseline-r1.*` is **attempt 1, the refusal** (`rc 4 open_failed`); `baseline-r1-rerun.*` is the run that produced `baseline-r1/` |
| `baseline-r{1,2}-arm-walk.log` | `event=arm_walk.*` per release |
| `journal-cursor-before-r1{,-rerun}.txt`, `-before-r2.txt` | systemd journal cursors bounding each round — the mechanism for re-reading fader evidence live off the box |
| `seat-level/seat-level-receipt.json` | Phase 2: converged, `-5.00 dB → 76.28 dB SPL`, `2026-08-24T21:51:22Z` |
| `refused-attempt/` | attempt 2's artifacts, quarantined *"so they are never confused with the real rounds"* (`run-log.md:601`) |
| `run-log.md` | the narrative record, 2,078 lines. The primary source for everything below |

**One round** = 8 routed captures over 5 poses (0°, +7°, −7°, +22°, −22°;
roles onax/onax/onax/offax/offax), `regime=per_driver`, `mover=arm`. The arm
walk logs **8 releases** and parks at 0° (`baseline-r2-arm-walk.log`). The
bank pulls the capture ring **whole** — 45 WAVs + 45 sidecars per round, not
just the 8 (`run-log.md:1334`). Both rounds: build `9fcda9ee5`, jts3 at
`192.168.1.92`, HiFiBerry DAC8x, Epique E150HE-44 woofer + B&C DE250-8 on a
190 mm R-OSSE waveguide, 2500 Hz LR4, UMIK-2 cal
`minidsp-minidsp_umik2-b7343c0c625b` (`page.py:711-731`).

**Two fader numbers, and confusing them will fail the run.** The measurement
frame is **−8.00 dB** (what the 16/16 holds at). The **household** value is
**−18.181818 dB** (what the standing park restores). The campaign is explicit
that drive differs between rounds — −17.514 vs −15.963 dBFS woofer — *"because
each round re-solves its own level from the ambient it measures; **the fader
frame, −8.00 dB, is identical**."*

### 1.4 The 7j notice, and how the re-mint clears it

**The box is deliberately non-measurable right now, and this is the state row 9
must clear.** Its end-state probe (`run-log.md:2013-2038`) — the same probe S11
cites as the definition of the standing park — records the last row as
**`blocked` / `active_baseline_topology_changed`**, marked *"the accepted
staleness."*

**Cause, proved on the box, not inferred** (`run-log.md:493-505`).
`driver_style` is **topology-owned** — `build_driver_safety_profile` reads it
off `topology.speaker_groups[].channels[].driver_style` — so it sits inside
`topology_config_fingerprint`. Entering the owner's fact rotated it:

| Topology | `topology_config_fingerprint` |
|---|---|
| Phase 0 (pre-edit) | `cd3fa7e090531579fb65034f1f37effc5cc0df4fb716f0f507a5aca6220ac3c2` — matches the applied baseline's snapshot |
| post-edit (`cone_driver`) | `edc887d26d223e92b215db1eb16bd313baf3f661ef87fa63945ce8bea0d8113d` — what the box now reports |

Chain: `driver_style` ∈ topology fingerprint → applied-baseline snapshot stale
→ `setup != ready` → the gate at
`jasper/web/correction_crossover_v2.py:6183` refuses → **a v2 measure session
will not open.** Deterministic; identical on every retry. The refusal the
campaign actually hit, verbatim:

```
round: open FAILED path=/correction/crossover/v2/session http=400
       detail=protected speaker setup is not ready; finish it before measuring
round finished: open_failed (rc 4)
```

**Wave 7j demoted this block, and it is DONE** — `b56ea4257` (#3006), wave-7
ledger at `:1106`. At HEAD the code is unambiguous:

| Piece | At HEAD |
|---|---|
| The constant | `jasper/active_speaker/_common.py:58-63` — `BASELINE_TOPOLOGY_CHANGED = "active_baseline_topology_changed"`, commented *"A DISCLOSURE, not a blocker (ruling S10, ADR-0019)"* |
| The severity | `setup_status.py:1115-1130` emits it as **`"warning"`**, never `"blocker"` |
| The doctor line | `topology changed since the applied baseline; re-mint when convenient` — rendered by `jasper/cli/doctor/audio.py:1673-1704`, check label **`active speaker setup notices`**, WARN, never fails the run |
| The `event=` | **`correction.crossover_v2_baseline_topology_stale`**, WARNING, `code=active_baseline_topology_changed` — `jasper/web/correction_crossover_v2.py:4683-4697`, **once per v2 session open**, not per `/state` poll |
| Readiness | `protected_ready` no longer reads `protected_topology_current` (`setup_status.py:1037-1059`); `blocked = bool(blockers)` counts blocker-severity only (`:1221-1222`), so `safety_muted`, `volume_allowed` and `grouping_allowed` all stay clean under a lone warning |

**The comparison that drives it** (`setup_status.py:1026-1036`): the applied
profile's frozen `source.topology_fingerprint` against a live recompute of
`topology_config_fingerprint(topology)`. That function hashes **the whole
topology dict minus `pairing_intent`** (`baseline_profile.py:241-247`) — its
docstring says "only fields that determine emitted DSP config", which is
aspirational. Fail-open: an empty fingerprint on either side fires nothing.

> **THE PLAN'S 7j ROW IS STALE ON ONE WORD, and it is the word that matters
> here.** `:1063` says *"entering `driver_style` — **metadata** — rotates
> `topology_config_fingerprint`"*. **`driver_style` is not metadata**, and 7j's
> own rider commit `62c5402f3` says so after review R1 refuted the original
> claim: it selects the tweeter's `min_highpass_hz`
> (`driver_protection.py:142-144`) and sits in the driver-safety target match
> (`driver_safety.py:3184-3196`), which is **role-independent — it fires for a
> woofer's `driver_style` too**. The fields that genuinely reach no clamp and no
> emitted filter are `SpeakerChannel.human_output_label` and a `SpeakerGroup`'s
> `label`. HEAD's own comment states it correctly at `setup_status.py:1050-1054`.
>
> **Consequence for row 9, and it is the sharpest thing in this brief:** after
> 7j the topology notice no longer stops a session — but
> `evaluate_driver_safety_profile` still can, **one gate downstream**, at
> `correction_crossover_v2.py:4715`, immediately after the 7j event fires, with
> `driver_safety_profile_target_mismatch`. Budgeting row 9 as "7j landed, so the
> box opens" is wrong. See gap 3.

**How the re-mint clears the notice.** Applying a baseline re-stamps
`source.topology_fingerprint` from the **live** topology
(`baseline_profile.py:418-431`, copied into the immutable
`recomposition_snapshot` at `:2804-2807`), so the two sides of §1.4's
comparison equalise. `persist_applied_baseline_profile`
(`baseline_profile.py:3299-3373`) is the **only** writer of the applied
artifact; three callers reach it — `_apply_baseline_profile_locked` (`:3874`),
`restore_applied_baseline_profile` (`:4075`), and `commissioning_apply.py:764`.

> **`jasper-active-speaker baseline-reemit` is the WRONG VERB — it refuses under
> exactly this condition.** It routes through `recompose_applied_baseline_yaml`
> → `applied_baseline_hardware_match` (`baseline_profile.py:2903-2916`), which
> returns a **blocker**, `applied_baseline_snapshot_topology_stale`: *"the
> applied active-speaker profile belongs to a different output topology; reapply
> speaker setup first."* A re-emit is gated **by** the staleness it looks like it
> would fix.

**The obligation's primary source is the campaign itself**
(`run-log.md:1985-1993`): *"**The box is deliberately left non-measurable until
the next campaign re-mints and applies.** A session that wants to measure must
clear this first…"* — **and the rest of that sentence is SUPERSEDED.** It
continues *"…and must not clear it by applying `55dee33aa48a`"*, because the
bare candidate carries `measured_group_ids: []` and would wipe the winner's
blend correction, tweeter linearization and level trim. **Owner ruling §6 R4,
2026-08-25, overrules exactly that** (`:1539-1553`): *"who cares if it wipes out
a tournament winner? We don't care about the tournament winner. In general, we
should apply stuff to jts3 when we have something we need to test."* R4 supplies
the framing that makes it safe to obey — this is a **QUALITY** unknown, never a
safety one, because MS-13's `_assert_program_graph_proven` refuses structurally
to return a program graph whose tweeter output lacks the high-pass and the
soft-clip limiter together. *"Never apply it because it is unproven"* is the
nanny class S10 abolished. **So: clear it however is convenient.** A fresh
session reading only the run-log will follow the older authority and lose an
hour.

### 1.5 The bench procedure

Written as a checklist because that is what it is. **Do not run any of it
without the S11 licence** — row 9 *is* sanctioned act 1, so the run itself is
licensed; nothing around it is.

**Preconditions (paper, do first).**

- [ ] Confirm the engine change under test has merged and CI is green. Row 9 is
      a merge gate for a wave, so know which wave you are gating.
- [ ] `git fetch origin && git merge-base --is-ancestor origin/main HEAD`.
- [ ] Confirm 7j is live on the build you are about to deploy (`b56ea4257` is
      an ancestor) — otherwise §1.4's block, not the notice, is what you meet.
- [ ] **Work from the main checkout, not an agent worktree.** `captures/` does
      not exist in a worktree (§0).

**Deploy.**

- [ ] `PI_HOST=jts3.local bash scripts/deploy-to-pi.sh` — the only deploy path
      (AGENTS.md non-negotiable 4). Never hand-roll rsync+install.
- [ ] Verify: `http://jts3.local/system/` shows the new SHA, and
      `ssh pi@192.168.1.92 sudo /opt/jasper/.venv/bin/jasper-doctor` is clean.
- [ ] Record the box SHA. The campaign's own note is worth copying: dry-runs
      ran **on the box's own runtime** (`/opt/jasper/.venv`) because the laptop
      worktree is a different SHA, *"and a laptop-side dry-run would have been a
      claim about a different build."*

**Re-mint the baseline (§1.4).** There is **no CLI for this** —
`jasper-active-speaker` exposes `startup-template`, `path-audit`, `path-probe`,
`environment-probe`, `runtime-safe-graph`, `baseline-reemit`, `commission-load`,
`commission-rollback` and `commission-ramp {step,ack,status,abort}`
(`jasper/cli/active_speaker.py:1420-1789`) and none of them mints. The surface
is the sound wizard, port **8784** (`jasper/web/sound_setup.py:6230`; nginx
proxies `/sound/setup/`), and it is a **two-POST fingerprint handshake**:

- [ ] `POST /sound/setup/active-speaker/baseline-profile` — compiles the
      candidate with `write=True` (`sound_setup.py:5783-5786`). Read
      `candidate_fingerprint` out of the response.
- [ ] `POST /sound/setup/active-speaker/baseline-profile/apply` with
      `{"expected_candidate_fingerprint": "<that>"}` — handler
      `_active_speaker_baseline_profile_apply_payload`
      (`sound_setup.py:5174-5228`), which calls `apply_baseline_profile` →
      `persist_applied_baseline_profile`. A mismatched fingerprint returns
      `status: "blocked"` / `baseline_candidate_fingerprint_mismatch`
      (`:5260-5283`). **This endpoint has zero JS callers — it is HTTP-only.**
      The UI's only wired button is `save-and-apply`
      (`deploy/assets/sound-profile/js/main.js:7438`), which does the same
      apply plus commissioning cleanup.
- [ ] Per R4, applying the bare candidate is permitted — the tournament winner
      is not precious and is recoverable from the banked artifacts.
- [ ] **Verify the re-mint took, two ways.** `jasper-doctor`'s
      `active speaker setup notices` line flips WARN → `ok` /
      `no standing speaker setup notices`. And `/state`'s
      `active_speaker_setup.protected_profile.topology_current` flips
      `False` → `True` (`setup_status.py:1082`, `:1278`).
- [ ] **Then check the gate one step downstream** — the driver-safety target
      match (`driver_safety.py:3184-3196`). If a v2 session still refuses, the
      code will be `driver_safety_profile_target_mismatch`, not the topology
      one, and the fix is to re-confirm the driver-safety profile against
      declaration revision 22. See gap 3.
- [ ] Record which graph is applied. §1.2: the entry-baseline summed capture is
      the one comparison that needs it, and the original ran against
      `candidate_7ac9583f15eb.yml`, sha `dcc90dabdc03adc9…`.

**Seat level (Phase 2 — re-run only if the mic or the room moved).**

```sh
ssh pi@192.168.1.92 'sudo -n /opt/jasper/.venv/bin/jasper-seat-level \
  --stimulus-wav /home/pi/seatlevel-noise-continuous.wav \
  --target-db-spl 77.5 --tolerance-db 2.5 \
  --mic-serial 810-8494 --json'
```

- [ ] The original converged `-5.00 → 76.28 dB SPL` and was **never
      recalibrated** across the campaign. Holding it is the like-for-like move;
      re-running it is a frame change and must be disclosed as one.

**Bank the journal cursor, then run each round.**

- [ ] Capture the journal cursor before each round, exactly as
      `journal-cursor-before-r{1,2}.txt` does. This is what makes the fader
      evidence re-readable off the live box afterwards.

```sh
PI_HOST=192.168.1.92 PI_USER=pi PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD \
/Users/jaspercurry/Code/JTS/.venv/bin/python scripts/run-crossover-round.py \
  --campaign /Users/jaspercurry/Code/JTS/captures/<new-campaign-dir> \
  --label baseline-r1 \
  --stage measure --tier remote \
  --angles 0,7,-7,22,-22 --regime per_driver \
  --attest-rig-clear --hostname jts3.local \
  --trail /Users/jaspercurry/Code/JTS/captures/<new-campaign-dir>/baseline-r1.trail.jsonl
```

- [ ] r2 is **the identical command with `--label baseline-r2`**. The
      campaign's binding constraints (`run-log.md:1290-1294`): **no
      `--complete-after`**, and *"In the held −8.00 frame. **NO re-level between
      rounds**"*.
- [ ] **Read the trail, not `$?`.** The campaign was bitten twice: a trailing
      `echo "RUNNER_EXIT=$?" | tee` makes the *pipeline* exit 0 while the round
      failed at `rc 4` (`run-log.md:459-463`, `:786-789`). Meaningful codes:
      `4 open_failed`, `5 walk_failed`, `11` refused-apply.
- [ ] A measurement run **never applies.** It prints the candidate fingerprint
      and stops. Applying is a second invocation naming that fingerprint.

**Score it.**

- [ ] **Fader hold — 16/16, 0 repairs.** Per round, over that round's journal
      window:

```sh
ssh pi@192.168.1.92 "sudo -n journalctl --after-cursor='<cursor>' \
  --until='<next round start>' -u jasper-correction-web -o short-iso" \
  | grep event=active_speaker.measurement_fader_drift
```

  Bar: 8 `held` per round across `check`/`measure`/`lateral`×5/`entry_baseline`,
  and **zero** `repairing`/`repaired`/`refused`. The windows must not overlap
  (the original: r1 `00:32:53–00:43:34 -04:00`, r2 `00:45:00–00:55:45 -04:00`).

- [ ] **The 0.37 dB curve comparison.** This is gap 2 below — there is no
      shipped command. Re-run the campaign-local chain:
      `report/derived/replay.py` → `curves.json` → `reduce.py` →
      `compact.json` → `build_page.py` → `graphs.html`, pointed at the new
      rounds *and* the sealed r1/r2. Bar: worst pair ≤ 0.37 dB over the ten
      driver-and-angle pairs.
- [ ] **Power/thermal sanity**, both rounds: `power.txt` must read
      `throttled=0x0`, zero under-voltage in `dmesg` and in the journal.

**Bank and park.**

- [ ] Bank into a **new campaign directory** — never write into
      `postfix-baseline-2026-08/`. The sealed r1/r2 is the reference and must
      stay byte-stable. Naming: `<topic>-<YYYY-MM>`, e.g.
      `acceptance-row9-2026-09`.
- [ ] Quarantine any refused or partial attempt under `refused-attempt/`, per
      the original's own discipline.
- [ ] **End with the standing park** (S11's operational form, and the probe it
      cites is the template):

| Probe | Bar |
|---|---|
| Playing | `spotify`/`airplay`/`usbsink` all `false` |
| CamillaDSP fader | back at the household value, `-18.181818` |
| Spool | `sessions` only |
| Audio units | `active active active` |
| Arm processes | **0** |
| `vcgencmd get_throttled` | `0x0` |
| Seat level | untouched |

- [ ] The `pgrep` trap, recorded because it cost the original a wrong reading:
      `pgrep -af "[a]rm_walk"` matched **its own ssh wrapper shell**, whose
      command line contained the literal string inside an `echo`. Print what
      matched before believing a live process.
- [ ] Write a `run-log.md` in the new campaign directory. The 0.37 dB number's
      only home is a report; if the reproduction's number has no home either,
      the next session cannot check it.

### 1.6 Three gaps that paper cannot close

**Gap 1 — the proving ground is not under version control, and the reference
scripts are not either.** `captures/` is gitignored. The four scripts that
produce the 0.37 dB figure live inside the gitignored campaign directory. The
intermediate `curves.json` is **already gone from disk** — only `compact.json`
survives — so the chain is not re-runnable from step 2 without re-running
`replay.py`. And `replay.py:21-24` **hardcodes an absolute path into a
different campaign directory** — verified, not relayed —
(`captures/flat-linearization-20260725/umik2-cal/umik2-b7343c0c625b.txt`).
Losing either directory loses the ability to compute the bar the same way
twice. *This is the standard §5 row 7 imposed on the line count — "the number is
produced the same way twice" — never imposed on the acoustics.* Whether those
scripts should be promoted into `scripts/` is an owner call, not a scout's.

**Gap 2 — the named tool cannot score the named round shape.** Acceptance
check (g) in the original campaign was `jasper-round-views repeat baseline-r1
baseline-r2`, and it errored: `baseline-r1: evidence packet carries no position
evidence`. Root cause proved on both rounds: `position_axis` /
`mark_distance_m` are written only by `cloud_position_record()` (cloud/verify
poses), and `repeat` hard-requires cloud position evidence plus a graded spec.
**A `--stage measure` round — the exact command the campaign prescribes — never
produces either.** The guard is still at HEAD:
`jasper/active_speaker/crossover_v2/round_views.py:243`. The conductor ruled
**re-scope, do not re-run** (`run-log.md:1630-1652`): *"(g) is answered by the
curve-level r1-vs-r2 comparison in `report/graphs.html` (the campaign noise
floor), not by a tool whose input contract this round shape cannot meet."*
Consequence for row 9: **the acceptance bar's instrument is a bespoke HTML
report, not a CLI.** Anyone budgeting row 9 as "run the runner twice, run the
tool" is budgeting a tool that will error.

**Gap 3 — WHICH gate the box is actually sitting behind is not determinable
from paper.** The re-mint *surface* is settled (the two-POST handshake above);
this gap is narrower and sharper. Two independent gates read `driver_style`,
and only one of them is 7j's subject:

| Gate | Code | 7j's effect |
|---|---|---|
| Topology staleness | `active_baseline_topology_changed` | **demoted to a warning.** No longer blocks |
| Driver-safety target match (`driver_safety.py:3184-3196`, hit at `correction_crossover_v2.py:4715`) | `driver_safety_profile_target_mismatch` | **untouched — this is 7j's declared carve** |

The campaign entered `driver_style` through *"the same owning two-step"* and
left the declaration at **revision 22, `confirmed` / `confirmed_and_current:
true`**, which *may* mean the safety profile already matches and only the
topology notice stands. The end-state probe was taken on a build predating 7j,
so it cannot distinguish the two. **Nothing in paper resolves it** — one
`jasper-doctor` run and one attempted session open on the re-deployed box will,
in under five minutes. Budget for the possibility that the re-mint alone is not
enough and the driver-safety profile needs re-confirming too.

*One thing NOT to do while resolving it:* the v2 state carries
`accepted_sound_declaration_change` / `accepted_sound_revision` /
`sound_declaration_undo`, which look like a seam for accepting a declaration
change without recompiling. The original session declined to explore it and was
right: *"hunting for a bypass around a refusal is exactly what the standing rule
forbids."*

### 1.7 One stale doc this brief will not edit

`docs/REFACTOR-2026-08.md:279-284` still instructs deploying *"**never
jts3**, which is the measurement bench holding a deliberate
`blocked/active_baseline_topology_changed` state (applying the bare
`55dee33aa48a` candidate would destroy the tournament winner's corrections)"*.
Both halves are dead: owner ruling R4 overrules the prohibition, and after 7j
`active_baseline_topology_changed` **cannot produce `blocked` at all** — it is
a `warning`. That file is the audit program's single-owner planning authority
(§6 R5's boundary table), so this brief flags it rather than editing across the
boundary. It wants one line from whoever owns that doc.

---

## 2. Row 10 — the instrument roster's status

### 2.1 What row 10 demands

Row 10 (`:1303`) is the owner's acceptance bar, quoted from the baseline
report: *"the full candidate campaign — many candidates, each measured, one
winner, and the winner re-measured."* Entire trusted range, multi-candidate,
best-of final, re-measured. It opens **only after every row above closes**, and
it **opens with instrument bring-up in roster order — R-1 → R-5a**, with
R-5b/R-6 when the owner provides hardware.

Two constraints the roster carries and this brief will not soften. **S11's
sanctioned acts stay exactly six** — the original five plus the act 6 adopted on
2026-08-26 — and the roster is post-acceptance and adds nothing to that list
(`:1456-1464`). And **an instrument is CODE; a preset is
DATA** (`:1341-1346`) — assembling `full-cloud` out of built instruments is a
parameter bundle, but building R-1…R-6 is engineering.

### 2.2 The status table at HEAD

Since the plan was written, **ruling S12's stub surface has SHIPPED**:
`jasper/active_speaker/crossover_v2/measure_spec.py` names each roster row in
code, declares its parameter, and returns a loud not-implemented disclosure
carrying a `captured` discriminator. That module is now the authoritative
statement of each instrument's code state, and its own docstring table
(`:28-40`) is the thing to read before the plan's prose.

The `captured` flag is the operationally load-bearing half: *"a stub whose
capture still happened has evidence waiting for the analysis that will read it,
and a stub whose capture did not happen has nothing banked and nothing to
re-analyze later."*

| # | What the row demands | State at HEAD | What remains |
|---|---|---|---|
| **R-1** | `measure(polarity=inverted)` + `analyze(null_depth)` | **Stub shipped, `captured=False`** — `INVERTED_POLARITY_NOT_IMPLEMENTED` (`measure_spec.py:78-81`; `_ROWS` at `:163-165`), rendering *"inverted-polarity capture not implemented; nothing captured, reverse-null pending R-1"*. The **decision** half is live: `crossover_alignment.py:61-62`'s `POLARITY_KEEP` / `POLARITY_INVERT` decide on measured null depth, and `:149-154` bank `in_phase_null_depth_db` / `reverse_null_depth_db` / `polarity_margin_db`. The **executor** `run_null_walk` was deleted for want of callers. **`PROBE_REVERSE_NULL = "P1"` (`attribution/closed_sets.py:81`) has never been run** | **both ends.** Make `polarity=inverted` play and bank, then write the `analyze(null_depth)` consumer. Nothing is banked to re-analyze, so this is a capture build first, not an analysis pass |
| **R-2** | wider orbit + first production caller for DI / per-angle | **Every asset built, zero production callers — re-verified at HEAD.** `flat_spec_views.directivity_table` and `forward_model.predict_sum` appear **only** in `tests/test_flat_spec_views.py` and `tests/test_crossover_forward_model.py`. Reach: `MOVER_MAX_ANGLE_DEG` (`angle_capture.py:196`) maps arm → `ARM_ENVELOPE_DEG`, human → `MAX_ANGLE_DEG`. `--angles` already accepts arbitrary whole degrees and has **no code default** — the angle set is caller-supplied. **The walk this program actually runs stops at ±22**: the r1/r2 arm-walk logs record exactly `0, 0, 0, +7, −7, +22, −22, 0`, and the plan reports the same of the shipped walk (`:1396`) | **a program change, not a build.** Widen the angle set and give the two computed models their first caller. **No stub exists and none is owed** — nothing is missing from the parameter surface |
| **R-3** | one `analyze` function over two record kinds that already exist | **Stub shipped, `captured=True`** — `NEAR_FIELD_SPLICE_NOT_IMPLEMENTED` (`measure_spec.py:76-78`), *"near-field splice not implemented; capture banked, splice pending R-3"*. `REGIME_NEAR_FIELD` declared (`contracts.py:1440`); `NEAR_FIELD_EXEMPT` already carves gating (`audio_measurement/gating.py:337`) | **the splice only.** The capture ships, so evidence accrues today and is re-analyzable the day the function lands. Cheapest row on the board |
| **R-4** | distortion-vs-**level** `measure` + the `analyze` consumer | **Stub shipped, `captured=True`** — `DISTORTION_VS_LEVEL_NOT_IMPLEMENTED` (`measure_spec.py:82-85`), *"distortion-vs-level sweep not implemented; capture banked, level ladder pending R-4"*: **every rung plays and banks its own record.** Supporting code exists — `audio_measurement/distortion.py`, `crossover_v2/harmonic_evidence.py`, `jasper-read-distortion` | **the consumer** that turns the banked set into a measured floor. The roster's warning is spent: **wave 7k is DONE** — the guide's Adoptions header now states the split and names the live slope-blind gates, so there is no stale claim left to import |
| **R-5a** | `measure(position_axis=vertical)` + a preset + MS-17's `prompt` | **Stub shipped, `captured=False`** — `VERTICAL_AXIS_NOT_IMPLEMENTED` (`measure_spec.py:86-90`), *"vertical-axis walk not implemented; nothing captured, pose prompts pending R-5a"*. `POSITION_AXIS_VERTICAL` is declared in `contracts.py`. Four sites still refuse deliberately: `position_angle_deg` raises, `pose_at_angle` calls elevation *"the ratified deferred axis"*, `CLOUD_VERIFY_POSE_PROMPTS` is *"vertical-free BY CONSTRUCTION"* (`angle_capture.py:378`), and `REMOTE_VERTICAL_DISCLOSURE` **tells the household the axis is not covered** | **undo three deliberate refusals and update the fourth**, or the speaker discloses a blind spot it no longer has. Mic-only, **not zero-code** |
| **R-5b** | the same parameters, driven by a positioner | **HARDWARE-GATED, unchanged.** Needs rig capability for elevation. Until then the blind spot stays stated and disclosed — S10's spirit applied to a measurement we cannot take | **owner decision only.** No code moves until the rig does |
| **R-6** | a sense-resistor jig feeding `measure` | **BOTH HALVES RULED — the plan's open decision is CLOSED and its count is stale.** The relay slot `build_bass_nearfield_spec` was **deleted** by #3081 (`056cc8cfc`, owner ruling 2026-08-26: *"delete now, re-add when the impedance hardware exists"*), taking `SHIPPED_KINDS`, `BUILDERS`, the re-export pair and its bass-nearfield relay test with it. The `bass_extension/profile.py:211` `impedance_import` slot **stays, under ADR-0018's park**. **Deliberately no stub** (S12: a stub for hardware that may never exist is the speculative flexibility the charter forbids) | **owner decision only.** The roster's *"two schema slots already wait"* is now **one**, and the *"decide once… silence resolves to deletion"* paragraph is spent — it was decided twice, and the two rulings agree |

### 2.3 Three things the roster's prose now gets wrong

1. **`:1356` — "two schema slots already wait."** One does.
2. **`:1404-1413` — R-6 framed as a live owner decision.** Closed by #3081 plus
   ADR-0018. The paragraph reads as pending work and is archaeology.
3. **`:1350` — R-1 cites `docs/attribution-stage-plan.md:349`.** The file moved
   to `docs/historical/` at #2979 (`b25216fff`). The *claim* holds exactly — the
   M1 row still reads *"Reverse-null (P1) **and** design-axis/vertical-offset
   (P5) — both **required**"* — but the path resolves nowhere.
   `crossover-design-guide-deep-research-2026-08-19.md:29` carries the same
   stale path in prose.

*None of the three changes what row 10 costs.* They are citation hygiene and
belong in whatever PR next touches §5 — not in this brief, which does not own
that file.

### 2.4 Two things the table shows that the ranked order does not

- **R-3 and R-4 are already accruing evidence.** Both stubs are `captured=True`,
  so every session run between now and their landing banks records the missing
  `analyze` function will be able to read. They get *cheaper* by waiting. R-1
  and R-5a bank nothing and do not.
- **R-2 is the only row that is purely a program change.** No new `measure`
  parameter, no `analyze` function that does not exist — two built models
  wanting their first caller, and a longer angle list. If the campaign wants an
  early win that is not R-1, it is this one.

---

## 3. The S11 amendment for the producer build

> **ADOPTED — owner-ratified 2026-08-26 (in-chat).** This section drafted the
> amendment; the owner signed it, and it is now **S11 act 6**, recorded in
> `REFACTOR-TUNING-2026-08.md` §4. The text below is the draft as written and as
> adopted — the plan's S11 row is the authoritative copy. The cost accounting in
> §3.3 stands unchanged, and it is the operative constraint now: **the act
> proves a producer, it does not build one, so nothing may run until the build
> in §3.3 lands.**

### 3.1 Why the existing five acts do not cover it

Wave 4g is the producer path, not the deletion path, because ruling S2 settled
#2202 as *fix*. Its blocker is not wiring — the plan's three greps say so, and
**all three re-verify at HEAD (`c253c3cf1`)**:

| 4g's claim | Re-derived at HEAD |
|---|---|
| `SummedCaptureProducer` is a runtime orphan | **holds.** Every reference is in `tests/test_active_speaker_commissioning_capture_producer.py` |
| `RawCaptureTransport` has exactly two references — its own definition and its own constructor parameter — and no production implementation | **holds exactly.** `commissioning_capture_producer.py:222` (the alias) and `:420` (the ctor param). Nothing else in the tree |
| `jts_active_driver_capture_admission_handoff` is the only `ADMISSION_HANDOFF_KIND`; there is no summed sibling | **holds.** `commissioning_admission.py:93`, used at `:210` and `:255` |
| `publish_complete_commissioning_evidence` has zero production callers (three test sites only) | **holds.** Definition at `commissioning_evidence_store.py:1160`; the three callers are all in `tests/` |
| No production `CommissioningTransition` emits `to_state="measured"` | **holds.** Two sites in the tree, both in `tests/test_active_speaker_commissioning_service.py` |
| The eligibility receipt has a production reader that denies on every call | **holds.** `read_commissioning_room_authority` (`commissioning_verification.py:753`), reached from `setup_status.py:1238-1240` |

So the re-arm is **a new build against the relay in the live lane's promoter
shape** — and the relay is the phone-mic capture path, a deliberately separate
trust boundary. That is why it needs a phone and a speaker, and why no amount of
unit testing finishes it: the thing being proven is that a summed capture
crosses a trust boundary, gets admitted, gets promoted, mints an
`ArtifactIdentity`, and comes back out of the reader.

**Act 4 is adjacent and is NOT this.** S11 act 4 is the #2202 **scoping hour**,
and §6 R8 is explicit that it is *"scoping, not commissioning"* — an hour on the
box to answer the design question *"what should the receipt say?"*, before wave
4 books an estimate. It authorises looking. It does not authorise a run.

And S11 said **"NO commissioning"** in terms. The producer proof is a
commissioning run. It was excluded by name, and 4g's own text conceded it —
*"which S11 excluded until a commissioning run was added to the sanctioned list
explicitly"* — which is exactly the condition the 2026-08-26 ratification met.
**S11's exclusion now reads "NO commissioning beyond act 6's single bounded
run."**

### 3.2 The amendment — drafted here, adopted 2026-08-26 as S11 act 6

> **(6) The commissioning producer proof.** *One* commissioning run on jts3,
> for the sole purpose of proving that the re-armed summed-capture producer
> writes the evidence its readers are waiting for. **It is an
> instrument-validation act, not a commissioning of the speaker** — the run's
> product is a receipt and a `complete.json`, never a tuning change.
>
> **Scope, and nothing outside it.** One speaker (jts3), one phone through the
> capture relay, one region. Permitted: capture, admit, promote, publish the
> `AdmittedCaptureProof` envelope, emit the `protected → measured` transition,
> write `runs/{run_id}/complete.json`, mint the eligibility receipt. **Not
> permitted: applying any candidate the run produces, any EQ or crossover change
> to the speaker's sound, a second region, a second speaker, or a retry loop.**
> A failed run ends and is reported; it is not re-attempted the same night.
>
> **Evidence required — five things, and the act closes only when all five
> read true.** (a) `capture_post_apply`'s proof is **published**, and
> `_reopen_capture` parses it with `AdmittedCaptureProof.from_mapping` and
> reaches every child through `reopen_artifact(identity)` — never by
> reconstructing a path. (b) A production `CommissioningTransition` emits
> `to_state="measured"`, which no site in the tree does today. (c)
> `publish_complete_commissioning_evidence` runs from production, and
> `commissioning_host.status()` **polls without raising** — the warning 4h says
> travels. (d) `read_commissioning_room_authority` returns something other than
> a denial, and the code it returns is named. (e) The bench ends with the
> standing park.
>
> **Bound.** One run. If the producer needs a second attempt, that is a second
> act and comes back for a second sanction. **The receipt RECORDS and never
> FORBIDS** (ruling S10): a receipt that cannot be produced leaves the lane
> working and says so loudly, so a failed act blocks nothing — it costs an
> evening and a report. **This act does not open acceptance row 10** and grants
> no tuning licence; the five acts plus this one remain the whole list.

### 3.3 What the proof procedure would cost

So the ruling is made with the bill visible, not just the wording.

**Before the bench, and this is the larger half.** The act is worthless until
the build exists, and the build is not a small one:

- **A production `RawCaptureTransport` implementation.** None exists. Nothing
  outside the producer's own test file has ever built a `RawCaptureResult`.
- **A summed admission handoff.** Production's only admitted-capture door is the
  **driver** relay door (`record_driver_capture` →
  `promote_isolated_driver_capture`). A summed sibling must be built; the v2
  cloud-position captures cannot be borrowed, because that lane uses
  `program_admission`, which mints **no `ArtifactIdentity`, no generation
  artifact, no playback artifact** — promoting one would mean fabricating the
  admission the receipt requires.
- **One missing write, and only one.** The post-apply prefixes are *not* in
  conflict and must not be "reconciled": the producer's
  `post-apply/{attempt_id}/{issuance_id}/{ordinal}` locates the three CHILD
  artifacts; the reader's `post-apply/{target_fingerprint}/repeat-{ordinal}.json`
  locates the proof ENVELOPE. Renaming either would be a no-op for the reader
  and would break the producer's own tests.
- **4h's `complete.json` writer rides along.** It cannot ship ahead of the
  transport without becoming the orphan class #3045 deleted.
- **A design answer, not just code.** §6 R8: wiring a producer means deciding
  what the eligibility receipt should *say*, which is a commissioning-eligibility
  design question. **That is act 4's job and act 4 should run first.**

**The bench evening itself.** Deploy via `scripts/deploy-to-pi.sh`; confirm the
SHA and a clean `jasper-doctor`; a phone on the capture relay; one region
captured and promoted; the five evidence checks in §3.2; the standing park. Call
it one evening, assuming the build is already merged and green.

**Quiet-hours note.** This act plays audible stimuli. It is daytime work or it
asks first.

### 3.4 If the owner had refused — the path not taken

*Kept as the recorded cost of the alternative; the owner adopted the act on
2026-08-26, so this branch is counterfactual.*

Refusal is a coherent answer and the plan already survives it. 4g's producer
half simply does not land during the refactor; the withdrawn −2,089-line
deletion stays withdrawn (it is already booked that way in the net-lines table
and §6 R8); the lane keeps working and keeps disclosing, exactly as S10
requires — since #3005 and #3029 a receipt that cannot be produced leaves the
lane working and says so loudly. **Nothing in acceptance rows 1–10 depends on
the producer.** The cost of refusing is that `read_commissioning_room_authority`
goes on denying, and 4h's six production readers go on being unreachable, until
the campaign era.

---

## 4. The chunk-3 order

**Row 9 does not wait for the cutover; row 10 waits on all of it.** Two
arguments from the plan's own text, then the honest caveat.

**(a) Row 9 is a per-wave merge gate, not an end gate.** §5's preamble
(`:1285-1288`): *"development gates on merging a wave, not runtime gates on the
speaker."* §3's standing sequence (`:496-503`) assigns it per wave — waves that
alter *"what the box actually does (4, 5, 6) take the acceptance run"*; waves
0, 2, 3, 7, 8 prove with the class-A suite. Waves 4 and 5 name it in **Verify**
(`:849`, `:917`) as due *before the wave closes*.

**(b) Row 9 is applied-graph-independent by structure, not by convenience** —
§6 R4 (`:1555-1565`), §1.2 above. The engine's *bindings* cannot move the
numbers it is measured against.

**Row 10 is the opposite and unambiguously so:** *"only after every row above
closes"* (`:1303`), and rows 4, 5 and 6 are the god-file targets — **chunk 2's
own deliverables** (its §6 names `__init__:2181-2957` as dissolving *"into
acceptance row 5's four destinations"*). **Row 10 waits on the full cutover.**

**The honest caveat: the plan's per-wave schedule has already been overtaken.**
Wave 5 is CLOSED (`:923`) though its Verify demanded the run first; waves
6d/6e/6f merged (`f6a6c56f3`, `5da40b9e2`, `b9738bf67`); and PR #3006 deferred
act 5 outright. **6d's bar was met rather than skipped** — its body's *"**Not
merged.** … the no-pop check on jts3 is the evidence that licenses it"* is
pre-merge staging text, and the check ran on 2026-08-26 and was banked on that
same PR before the merge. **So two of S11's six acts are unrun — 1 (row 9) and 5
(7j's verification), with no record of either in `captures/`** — and act 2 is a
banked bounded PASS. This is not a violation to litigate.
The plan flagged its own split as *"the conductor's scoping of the rule, not the
owner's words — say so, and let the owner collapse it if he meant a hardware run
every time"* (`:501-503`), and the owner's sentence reads the other way: *"We'll
get that done and then we'll start measuring on hardware once everything's
landed."* Execution followed the owner.

**What act 2's banked PASS covers, and what it leaves open.** The act's bar is
*"swap the graph inside an open measurement window with the fader parked and a
recorder running, and listen"* (`REFACTOR-TUNING-2026-08.md:1055-1057`). The run
met every clause of it, and met the last one with a **recorded artifact rather
than an ear-claim**: UMIK-2 continuous capture at 48k/S32_LE while the *shipped*
`MeasurementSessionGraph` (deployed `f9d7c81`) performed both un-ducked swaps
plus a ducked in-recording control. The load-bearing statistic is the
**sample-to-sample delta** — room noise can raise a peak but cannot produce a
one-sample step — and every swap window sits **7–8 dB below the ambient floor's
own delta** (un-ducked install −8.3, restore −7.7, ducked control −7.5):
un-ducked is indistinguishable from ducked is indistinguishable from silence. The
swaps were separately proven real (`confirm_graph_is_live` true; un-ducked reload
1.9 ms), and the duck cost measured **453.8 ms/swap** against the plan's predicted
Δ2 ≈ 454 ms, which independently corroborates the number 6d was priced on.

**The bound, declared by the run and not closed by it: every swap re-applied the
box's own current graph.** jts3 carries a `protection_required` compression
driver with no committed crossover, so no *program* graph gets synthesized for
it, and **no swap between two different graphs is on record.** The run argues the
bound is physically empty — a session swaps only in silence (install before the
first stimulus, restore after the last), and in silence a gain difference makes
no sound, leaving the reload discontinuity as the only noise mechanism, which is
exactly what was measured. **That argument is the owner's to accept or to send
back, and this brief does not mark the act complete.** Note what a re-run would
cost before reopening it: a second graph to swap to means a committed crossover
on jts3, and S11 forbids crossover changes to the box's sound until acceptance
closes — so the bound may not be closable on this box during the refactor at all.

*Evidence, by name.* The sanctioned-act record and the merge disposition are both
comments on **PR #3111** (2026-08-26T16:25:58Z and T18:58:32Z); the wave's own
deletion argument is in its body. It is restated outside that PR in **#3137**
(6e)'s wave-closing disposition, which banks the same two numbers. There is **no
`captures/` directory for this act** — the duck material in
`captures/tuning-stack-inventory-2026-08/08-lane-stereo-and-duck-evidence.md` is
the 2026-08-25 crux-8 bench run on build `9fcda9ee5`, a different run from this
one (2026-08-26, build `f9d7c81`). *(One line this brief flags
rather than edits, per §1.7's boundary rule: `REFACTOR-TUNING-2026-08.md:1057`
still reads "It is still unrun" of this act. It wants one line from whoever owns
that doc.)*

| Gate | Blocks row 9? | Blocks row 10? |
|---|---|---|
| The cutover (chunk 2, W1–W5) | **No** — (a) and (b) | **Yes** — rows 4, 5, 6 are its deliverables |
| 7j on the deployed build, then the re-mint | **Yes** — without both, no session opens (§1.4) | transitively |
| The 4g/4h producer build + a sanctioned act 6 | **No** — nothing in rows 1–10 depends on it (§3.4) | **No** |
| Chunk 2's two open acceptance questions | **No** — claim/counting, not measurement | **Yes** — row 3, and the plan's analysis-unit claim (which is not an acceptance row; see below) |

**Two items this brief inherits and does not own.** Chunk 2 found row 3's
*"`DriverResponse` banked"* *"satisfied in spirit for one of three kinds and
unsatisfied for two"* and says to record it as an **open acceptance question**;
and the plan's "92 analysis units" **cannot be reproduced at HEAD**. **W2-d
settles the second one**, and the method it commits to is: the **20 produced
`ProgramAnalysis` fields** that `tests/test_program_analysis_field_census.py`
counts from source, grouped by gate into **15 units** whose table W2-a carries.
The truth layer's two package roots gain a no-upward-import pin at the same
time. Nothing further is owed on it. *(One correction rides with this: the row
reference this line used to carry was wrong. Acceptance row 3c is **Front-end
sharing (MS-17)**, and the analysis-unit claim is **not an acceptance row at
all** — it lives in the plan's §0 non-goals bullet and its §1 diagram, which is
where W2-d restates it.)* Both are docs work, both are in chunk 2's tier 0,
**neither touches a microphone.** Separately: **the 4g/4h producer build is on no DAG** —
chunk 2's §7 contains no producer item and the document never mentions 4g or 4h.
It is chunk-1 residual that neither chunk owns. §3 asks the licence question;
who schedules the build is a different open question this brief does not answer.

**The owner's queue was three items, and it is now empty.** #3151 settled chunk
2's §6 decisions and left two things waiting on him — **6.2's seam brief** and
**Appendix A's charter question**; §3's act 6 was the third. All three were
ruled in chat on **2026-08-26**: 6.2 **FOLDs**, Appendix A **drops the
attribution**, and act 6 is **adopted**. They were independent — none blocked
another — and only act 6 touches hardware.

### The recommendation

**Batch the two unrun acts into one bench evening** — same box, same deploy,
and their order is a dependency chain rather than a list:

```
deploy (bash scripts/deploy-to-pi.sh, PI_HOST=jts3.local)
  └─ ACT 5   7j's demotion verification — a metadata edit no longer blocks a
     │       measure session, and the doctor line appears. This is also gap 3's
     │       determination: it tells you which gate the box is behind.
     └─ re-mint (§1.5)
        └─ ACT 1  row 9, round r1
           │      └─ (ACT 2  banked bounded PASS, 2026-08-26 — carried only if
           │                 the owner reopens its bound, and then INSIDE r1's
           │                 open measurement window)
           └─ ACT 1  row 9, round r2 (no re-level between rounds)
              └─ the standing park
```

Act 5 first because it says whether the session opens at all. **Act 2 is a
conditional node, not a required one:** its PASS is already banked, so the
evening is two acts unless the owner reopens the bound — and if he does, the
re-run needs an open measurement window that r1 already provides, so it rides r1
rather than opening a session twice. One evening either way.

**Then gate the next box-behaviour change on row 9's result.** §3's own rule
now points at chunk 2's join node **W5-b (`TuningSession` in production)** and
at **W1-c (the retention lift)** — the items that change how the box measures
and what it banks. **Row 10 opens when the cutover finishes and rows 1–8 read
true**, beginning with R-1 (§2).

**What the evening does not settle.** The proving ground exercises no failure
branch (`:542`, S7). A green row 9 licenses the happy path and nothing else; past
that line it is the class-A suite and the invariant→pin table that carry the
weight, not the microphone.
