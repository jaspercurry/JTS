# The acceptance brief: what row 9 and row 10 actually demand

> **THIS DOCUMENT DIES WHEN ACCEPTANCE PASSES.** It is a readiness brief, not a
> planning authority. The bars belong to
> [`REFACTOR-TUNING-2026-08.md`](REFACTOR-TUNING-2026-08.md) §5; this file only
> says what state we are in against them, what a bench operator would actually
> type, and what the paper cannot answer. When row 9 closes and row 10 opens,
> delete it — the roster and the campaign live in §5, and the procedure will
> have been run.

> **Chunk 3 of the tuning refactor.** Chunk 1 — waves 0–8 of
> [`REFACTOR-TUNING-2026-08.md`](REFACTOR-TUNING-2026-08.md) §3 — built the
> engine beside the god files. Chunk 2 —
> [`REFACTOR-CUTOVER-2026-08.md`](REFACTOR-CUTOVER-2026-08.md) — plans the
> cutover. This chunk plans the **proof**.
>
> **Nothing here is a hardware licence.** Ruling S11's five sanctioned acts are
> still five. §3 *drafts* a sixth for the owner to sign or refuse; drafting it
> does not add it.

**STATUS — scouted at `c253c3cf1`, paper only. No hardware was touched.**

| § | Section | Status |
|---|---|---|
| 0 | Premise re-derivation ledger | VERIFIED-COMPLETE |
| 1 | Row 9 — the baseline re-park | VERIFIED-COMPLETE, three gaps flagged |
| 2 | Row 10 — the instrument roster's status | VERIFIED-COMPLETE |
| 3 | The S11 amendment for the producer build | DRAFTED — awaits the owner |
| 4 | The chunk-3 order | VERIFIED-COMPLETE |

---

## 0. Premise re-derivation ledger

Everything this brief was handed was re-derived at `c253c3cf1`. What moved is
recorded rather than silently corrected.

| Premise as handed | At HEAD | Disposition |
|---|---|---|
| The banked r1/r2 baselines are in the repo | **`captures/` is gitignored** (`.gitignore:38-40`, no trailing slash); `git ls-files captures/` returns zero rows. The campaign exists **only** in the main checkout at `/Users/jaspercurry/Code/JTS/captures/postfix-baseline-2026-08/`, mode `0700`/`0600` | **corrected, and it is load-bearing.** An agent worktree cannot see the proving ground at all. §1 gap 1. |
| "Two schema slots already wait" for R-6 (`:1356`) | **one.** `capture_relay.build_bass_nearfield_spec` was **deleted** by #3081 (`056cc8cfc`, 2026-08-26) with its `SHIPPED_KINDS`/`BUILDERS` entries and re-exports. `bass_extension/profile.py:211`'s `impedance_import` survives under **ADR-0018**'s park | **STALE — the roster line overcounts.** §2 R-6. |
| R-6 is an open owner decision ("silence resolves to deletion", `:1404-1413`) | **settled twice, in opposite-looking ways that are actually one answer.** #3081 deleted the relay half by owner ruling; ADR-0018 parked the `bass_extension/` half by owner ruling | **CLOSED.** §2 R-6. |
| #1738 is a live binary owner call — *"wire it up, or delete it"* (`:1268-1272`) | **ADR-0018: the owner chose a third option — PARKED.** *"I want to leave it parked."* | **CLOSED.** The plan's §4 tail is stale on this. |
| R-1 cites `docs/attribution-stage-plan.md:349` (`:1350`) | that file is at **`docs/historical/attribution-stage-plan.md`** since #2979 (`b25216fff`). The *content* holds — the M1 row still reads *"Reverse-null (P1) **and** design-axis/vertical-offset (P5) — both **required**"* | **path stale, claim intact.** §2 R-1. |
| Wave 7k (the guide's stale Adoptions header) is owed | **DONE.** `crossover-design-guide-deep-research-2026-08-19.md:1-45` now states the split explicitly and names the live slope-blind gates | **closed.** |
| The report's bar reads *"anything smaller than about 0.4 dB is noise, not a result"* (`:1302`) | source is *"any future **'improvement'** smaller than about 0.4 dB is noise, not a result"* (`report/derived/page.py:246-250`) | **paraphrase, not quote.** The plan renders it inside quote marks. Substance identical; the word `improvement` scopes it to deltas, which matters when someone points it at an absolute. |
| Wave 6d landed with the no-pop check as its licensing evidence | 6d **merged** at `f6a6c56f3` (#3111). Its own body says *"**Not merged.** This is the staging point: it wants the adversarial pass, and the no-pop check on jts3 is the evidence that licenses it"* — and **no no-pop record exists** in `captures/` | **OPEN, and it is not row 9's problem to fix.** §4. |

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

The row's own framing matters as much as the numbers: it is *"an
instrument-validation act, not a measurement campaign"* (S11), and §5's
preamble scopes it as **a development gate on merging a wave, never a runtime
gate on the speaker** (`:1285-1288`).

**And row 9 is not a whole-engine proof.** The plan says so itself twice: *"the
proving ground exercises none of these"* — the failure-branch pins, refusals,
races, cancellation, retry, stall recovery (`:542`, ruling S7 at `:1244`). A
green r1/r2 says the happy path measures the same. It says nothing about the
paths S7 licensed default-aggressive deletion around.

### 1.2 What the row does NOT demand — the like-for-like carve

The **per-driver reproduction rides the session measurement graph, not the
applied production graph** (`:1555-1565`). `PHASE_CHECK`, `PHASE_MEASURE`,
`PHASE_LATERAL` are exactly the phases that pay the swap today and ride the
session graph after wave 6. So **applying a candidate to jts3 cannot move the
numbers row 9 is measured against.**

One comparison IS applied-graph-dependent and gets a note, not a prohibition:
the **entry-baseline summed capture**. Compare it against the same applied
graph as the original, **or disclose the delta** (row 9's own text, §6 R4 at
`:1567-1570`).

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
must clear.** Its end-state probe (`run-log.md:2013-2038`) — the same probe
S11 cites as the definition of the standing park — records the last row as:

| Probe | Value | Expected |
|---|---|---|
| Protected setup | **`blocked` / `active_baseline_topology_changed`** | *the accepted staleness* |

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

**Wave 7j demotes this block** (`:1063`, ADR-0019's closing paragraph names it):
playback continues on the applied graph, measuring continues, and the fact
becomes *"one loud `event=` plus a doctor line"*. The narrow carve is a
**declared-CAP change that makes the currently-applied graph exceed the new
limit**; metadata never gates. **7j is DONE** — `b56ea4257` (#3006), per the
wave-7 ledger at `:1106`.

**How the re-mint clears the notice.** Once 7j has demoted the block, what
remains is a *disclosure* comparing the applied baseline's stored topology
fingerprint against the live one. Re-minting and applying a baseline compiled
against the current topology makes the two equal, and the disclosure stops
firing. **That is the mechanism; the campaign's own words are the primary
source for the obligation** (`run-log.md:1985-1993`):

> **The box is deliberately left non-measurable until the next campaign
> re-mints and applies.** A session that wants to measure must clear this
> first…

**and the rest of that sentence is SUPERSEDED — do not follow it.** The
run-log continues *"…and must not clear it by applying `55dee33aa48a`"*, on the
grounds that the bare candidate carries `measured_group_ids: []` and would wipe
the winner's blend correction, tweeter linearization and level trim. **Owner
ruling §6 R4, 2026-08-25, overrules exactly that** (`:1539-1553`):

> *"who cares if it wipes out a tournament winner? We don't care about the
> tournament winner. In general, we should apply stuff to jts3 when we have
> something we need to test."*

R4 adds the safety framing that makes it safe to obey: this is a **QUALITY**
unknown, never a safety one — MS-13's `_assert_program_graph_proven` refuses to
return a program graph whose tweeter output lacks the high-pass and the
soft-clip limiter together, structurally, on every compiled graph. *"Never
apply it because it is unproven"* is the nanny class S10 abolished.

**So: clear it however is convenient.** The prohibition in the run-log's
closing section is the older authority and a fresh session reading only that
file will follow it and lose an hour.

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

**Clear the topology staleness (§1.4).**

- [ ] Re-mint and apply a baseline against the current topology (declaration
      revision 22, woofer `driver_style = cone_driver`). Per R4, applying the
      bare candidate is permitted — the tournament winner is not precious and
      is recoverable from the banked artifacts.
- [ ] Confirm the protected setup reads `ready`, not
      `active_baseline_topology_changed`. **This is the actual gate** —
      `/correction/crossover/v2/session` returns `http=400` otherwise and the
      runner exits `rc 4`.
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
`replay.py`. And `replay.py:22-25` **hardcodes an absolute path into a
different campaign directory**
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

**Gap 3 — how much of the re-mint is a *procedure* is undetermined from
paper.** §1.4 establishes what the notice is, what rotates it, and that a
re-mint-and-apply clears it. What paper does **not** settle is the exact
operator surface at HEAD after 7j: which page, endpoint or CLI mints and
applies a measured baseline in one step, and whether the v2 seam the campaign
spotted — `accepted_sound_declaration_change` / `accepted_sound_revision` /
`sound_declaration_undo` — is now the intended path. The original session
declined to explore it, correctly: *"hunting for a bypass around a refusal is
exactly what the standing rule forbids."* **Determine this at the box in the
first ten minutes of the run, not by reading more.** It is the one checklist
line above that is written as an outcome rather than a command.
